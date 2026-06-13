"""Resolve full job descriptions from alert rows using safe public sources.

Pipeline position:
job_alerts_raw.csv -> jd_resolver.py -> job_posts_enriched.csv -> prepare_job_posts.py

This is a resolver, not a LinkedIn bypass:
- direct public URL fetch when allowed by robots.txt
- public search to find company careers / ATS duplicates
- ATS API adapters for common public boards
- Schema.org JobPosting / visible-page extraction fallback
- no login, cookies, browser automation, or anti-bot bypass
"""

import argparse
import csv
import json
import re
import sys
import time
import os
import urllib.error
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import supercrawler


BASE_COLUMNS = supercrawler.BASE_COLUMNS
ENRICH_COLUMNS = supercrawler.ENRICH_COLUMNS
RESOLVER_COLUMNS = [
    "jd_resolver_stage",
    "jd_resolver_source_kind",
    "jd_search_queries",
    "jd_candidate_urls",
    "jd_attempted_urls",
    "jd_rejected_urls",
]
REVIEW_COLUMNS = BASE_COLUMNS + ENRICH_COLUMNS + RESOLVER_COLUMNS

ATS_HOST_KEYWORDS = {
    "greenhouse.io",
    "job-boards.greenhouse.io",
    "boards.greenhouse.io",
    "lever.co",
    "jobs.lever.co",
    "smartrecruiters.com",
    "jobs.smartrecruiters.com",
    "myworkdayjobs.com",
    "workdayjobs.com",
    "pageuppeople.com",
    "successfactors.com",
    "oraclecloud.com",
    "taleo.net",
    "icims.com",
    "jobadder.com",
    "ashbyhq.com",
    "recruitee.com",
    "workable.com",
}

AGGREGATOR_HOSTS = {
    "linkedin.com",
    "seek.com.au",
    "indeed.com",
    "au.indeed.com",
    "jora.com",
    "glassdoor.com",
    "adzuna.com.au",
}

BAD_SEARCH_HOSTS = {
    "google.com",
    "bing.com",
    "duckduckgo.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "reddit.com",
}

CAREER_PATH_TERMS = {
    "career",
    "careers",
    "job",
    "jobs",
    "join-us",
    "work-with-us",
    "vacancies",
    "opportunities",
}

FULL_STATUSES = {
    "jd_full_input",
    "jd_full_official_ats_api",
    "jd_full_official_jsonld",
    "jd_full_official_page_text",
    "jd_full_aggregator_match",
    "jd_full_apify_exact_id",
}

SEARCH_PROVIDERS = {"auto", "none", "brave", "google_cse", "bing", "serpapi", "duckduckgo_html"}


@dataclass
class ResolvedJob:
    data: supercrawler.JobData = field(default_factory=supercrawler.JobData)
    stage: str = ""
    source_kind: str = ""
    search_queries: list = field(default_factory=list)
    candidate_urls: list = field(default_factory=list)
    attempted_urls: list = field(default_factory=list)
    rejected_urls: list = field(default_factory=list)


def clean_text(value):
    return supercrawler.clean_text(value)


def lower_ascii(value):
    return supercrawler.lower_ascii(value)


def unique(values):
    return supercrawler.unique(values)


def url_host(url):
    return supercrawler.url_host(url)


def normalize_company(value):
    drop = {
        "pty", "ltd", "limited", "inc", "group", "australia", "australian",
        "anz", "co", "company", "corp", "corporation",
    }
    return [t for t in re.findall(r"[a-z0-9]+", lower_ascii(value)) if t not in drop]


def registrable_domain(host):
    host = (host or "").lower().split(":")[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = [p for p in host.split(".") if p]
    while labels and labels[0] in {"www", "careers", "career", "jobs", "job"}:
        labels = labels[1:]
    if len(labels) >= 3 and labels[-2] in {"com", "net", "org", "gov", "edu", "co"} and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host


def host_matches_domain(host, domain):
    if not host or not domain:
        return False
    return registrable_domain(host) == registrable_domain(domain)


def source_kind(url, row):
    host = url_host(url)
    root = registrable_domain(host)
    all_hosts = {host, root}
    if any(h in ATS_HOST_KEYWORDS for h in all_hosts):
        return "official_ats"
    if row.get("company_domain") and host_matches_domain(host, row.get("company_domain", "")):
        return "official_company"
    if any(h in AGGREGATOR_HOSTS for h in all_hosts):
        return "aggregator"
    company_tokens = normalize_company(row.get("company", ""))
    if company_tokens and any(token in host.replace("-", "") for token in company_tokens[:2]):
        path = urllib.parse.urlsplit(url).path.lower()
        if any(term in path for term in CAREER_PATH_TERMS):
            return "possible_company"
    return "unknown"


def is_allowed_candidate(url, row, include_aggregators=False):
    host = url_host(url)
    root = registrable_domain(host)
    if not host or host in BAD_SEARCH_HOSTS or root in BAD_SEARCH_HOSTS:
        return False
    kind = source_kind(url, row)
    if kind == "aggregator" and not include_aggregators:
        return False
    return kind in {"official_ats", "official_company", "possible_company", "aggregator"}


def query_quote(value):
    value = clean_text(value)
    return f'"{value}"' if value else ""


def build_search_queries(row, max_queries=8):
    title = row.get("role_title", "")
    company = row.get("company", "")
    location = row.get("location", "")
    domain = row.get("company_domain", "")
    base = " ".join(v for v in [query_quote(title), query_quote(company), query_quote(location), "careers OR jobs"] if v)
    queries = [base]
    if domain:
        queries.append(f'{query_quote(title)} site:{domain}')
    queries.extend([
        f'{query_quote(title)} {query_quote(company)} greenhouse',
        f'{query_quote(title)} {query_quote(company)} lever',
        f'{query_quote(title)} {query_quote(company)} smartrecruiters',
        f'{query_quote(title)} {query_quote(company)} workday',
        f'{query_quote(title)} {query_quote(company)} "JobPosting"',
        f'{query_quote(title)} {query_quote(company)} "apply"',
    ])
    return [q for q in dict.fromkeys(q for q in queries if q.strip())][:max_queries]


def search_url(query):
    return "https://duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)


def request_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body), f"http {getattr(resp, 'status', 200)}"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:250]
        return None, f"http {exc.code}: {detail}"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return None, f"search api error: {exc}"


def select_search_provider(provider):
    provider = provider or "auto"
    if provider != "auto":
        return provider
    if os.environ.get("BRAVE_SEARCH_API_KEY"):
        return "brave"
    if os.environ.get("GOOGLE_CSE_API_KEY") and os.environ.get("GOOGLE_CSE_ID"):
        return "google_cse"
    if os.environ.get("BING_SEARCH_API_KEY"):
        return "bing"
    if os.environ.get("SERPAPI_API_KEY"):
        return "serpapi"
    return "none"


def search_api_links(query, provider):
    provider = select_search_provider(provider)
    if provider == "none":
        return [], "no search provider configured"
    if provider == "brave":
        key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
        if not key:
            return [], "BRAVE_SEARCH_API_KEY missing"
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({
            "q": query,
            "count": "10",
            "country": "AU",
            "search_lang": "en",
        })
        data, status = request_json(url, headers={"Accept": "application/json", "X-Subscription-Token": key})
        results = (((data or {}).get("web") or {}).get("results") or [])
        return [r.get("url", "") for r in results if r.get("url")], status
    if provider == "google_cse":
        key = os.environ.get("GOOGLE_CSE_API_KEY", "")
        cx = os.environ.get("GOOGLE_CSE_ID", "")
        if not key or not cx:
            return [], "GOOGLE_CSE_API_KEY or GOOGLE_CSE_ID missing"
        url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode({
            "key": key,
            "cx": cx,
            "q": query,
            "num": "10",
            "gl": "au",
        })
        data, status = request_json(url)
        return [r.get("link", "") for r in (data or {}).get("items", []) if r.get("link")], status
    if provider == "bing":
        key = os.environ.get("BING_SEARCH_API_KEY", "")
        if not key:
            return [], "BING_SEARCH_API_KEY missing"
        url = "https://api.bing.microsoft.com/v7.0/search?" + urllib.parse.urlencode({
            "q": query,
            "count": "10",
            "mkt": "en-AU",
        })
        data, status = request_json(url, headers={"Ocp-Apim-Subscription-Key": key})
        results = ((data or {}).get("webPages") or {}).get("value") or []
        return [r.get("url", "") for r in results if r.get("url")], status
    if provider == "serpapi":
        key = os.environ.get("SERPAPI_API_KEY", "")
        if not key:
            return [], "SERPAPI_API_KEY missing"
        url = "https://serpapi.com/search.json?" + urllib.parse.urlencode({
            "engine": "google",
            "q": query,
            "api_key": key,
            "gl": "au",
            "hl": "en",
        })
        data, status = request_json(url)
        return [r.get("link", "") for r in (data or {}).get("organic_results", []) if r.get("link")], status
    return [], f"unsupported search provider {provider}"


def discover_candidates(row, fetcher, max_queries=8, max_results=12, include_aggregators=False, search_provider="auto"):
    queries = build_search_queries(row, max_queries=max_queries)
    candidates = []
    attempted = []
    rejected = []
    provider = select_search_provider(search_provider)
    for query in queries:
        if provider == "duckduckgo_html":
            url = search_url(query)
            attempted.append(url)
            content, status = fetcher.fetch(url)
            if content is None:
                rejected.append(f"{url} [{status}]")
                continue
            links = supercrawler.extract_search_result_links(url, content)
            rejected_status = status
        else:
            attempted.append(f"{provider}:{query}")
            links, rejected_status = search_api_links(query, provider)
            if not links:
                rejected.append(f"{provider}:{query} [{rejected_status}]")
                continue
        for link in links:
            if is_allowed_candidate(link, row, include_aggregators=include_aggregators):
                candidates.append(link)
            else:
                rejected.append(f"{link} [low-priority search result]")
        if len(unique(candidates)) >= max_results:
            break
    ranked = rank_candidates(unique(candidates), row)
    return queries, ranked[:max_results], attempted, rejected


def rank_candidates(urls, row):
    def score(url):
        kind = source_kind(url, row)
        host = url_host(url)
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lower()
        s = {
            "official_ats": 80,
            "official_company": 75,
            "possible_company": 55,
            "aggregator": 35,
            "unknown": 0,
        }.get(kind, 0)
        if any(term in path for term in CAREER_PATH_TERMS):
            s += 12
        title_tokens = set(supercrawler.token_set(row.get("role_title", "")))
        url_tokens = set(re.findall(r"[a-z0-9]+", lower_ascii(path + " " + host)))
        s += min(len(title_tokens & url_tokens) * 3, 15)
        return s
    return sorted(urls, key=score, reverse=True)


def fetch_json(fetcher, url):
    content, status = fetcher.fetch(url)
    if content is None:
        return None, status
    try:
        return json.loads(content), status
    except json.JSONDecodeError:
        return None, f"{status}; invalid json"


def greenhouse_api_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    query = urllib.parse.parse_qs(parsed.query)
    board = ""
    job_id = ""
    if "boards.greenhouse.io" in host or "job-boards.greenhouse.io" in host:
        parts = path.split("/")
        if parts:
            board = parts[0]
        if "jobs" in parts:
            idx = parts.index("jobs")
            if idx + 1 < len(parts):
                job_id = parts[idx + 1]
    if "embed/job_app" in path:
        board = (query.get("for") or [""])[0]
        job_id = (query.get("token") or query.get("gh_jid") or [""])[0]
    job_id = (query.get("gh_jid") or [job_id])[0]
    if board and job_id:
        return f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=false"
    return ""


def lever_api_url(url):
    parsed = urllib.parse.urlsplit(url)
    if "jobs.lever.co" not in parsed.netloc.lower():
        return ""
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) >= 2:
        return f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}"
    return ""


def smartrecruiters_api_url(url):
    parsed = urllib.parse.urlsplit(url)
    if "smartrecruiters.com" not in parsed.netloc.lower():
        return ""
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) >= 2:
        posting_id = parts[1].split("-", 1)[0]
        return f"https://api.smartrecruiters.com/v1/companies/{parts[0]}/postings/{posting_id}"
    return ""


def workday_api_url(url):
    parsed = urllib.parse.urlsplit(url)
    if "myworkdayjobs.com" not in parsed.netloc.lower() and "workdayjobs.com" not in parsed.netloc.lower():
        return ""
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) >= 4 and parts[0].lower() in {"en-us", "en", "en-au"} and parts[2] == "job":
        tenant = parsed.netloc.split(".")[0]
        site = parts[1]
        job_path = "/".join(parts[3:])
        return f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/job/{job_path}"
    return ""


def description_from_lever(item):
    parts = [
        item.get("descriptionPlain") or item.get("description") or "",
        item.get("additionalPlain") or item.get("additional") or "",
    ]
    for section in item.get("lists") or []:
        parts.append(section.get("text", ""))
        parts.append(section.get("content", ""))
    return clean_text(" ".join(parts))


def jobdata_from_greenhouse(item, source_url):
    data = supercrawler.JobData(source_url=source_url, status="jd_full_official_ats_api", confidence="high")
    data.title = clean_text(item.get("title", ""))
    data.company = clean_text(item.get("company_name", ""))
    loc = item.get("location") or {}
    data.location = clean_text(loc.get("name", "") if isinstance(loc, dict) else loc)
    data.posted_date = clean_text(item.get("updated_at", ""))
    data.description = clean_text(item.get("content", ""))
    data.notes.append("Greenhouse public Job Board API")
    return data


def jobdata_from_lever(item, source_url):
    data = supercrawler.JobData(source_url=source_url, status="jd_full_official_ats_api", confidence="high")
    data.title = clean_text(item.get("text", ""))
    data.company = clean_text(item.get("hostedUrl", "").split("/")[3] if item.get("hostedUrl") else "")
    cats = item.get("categories") or {}
    data.location = clean_text(cats.get("location", ""))
    data.employment_type = clean_text(cats.get("commitment", ""))
    data.description = description_from_lever(item)
    data.notes.append("Lever public Postings API")
    return data


def jobdata_from_smartrecruiters(item, source_url):
    data = supercrawler.JobData(source_url=source_url, status="jd_full_official_ats_api", confidence="high")
    data.title = clean_text(item.get("name", ""))
    company = item.get("company") or {}
    data.company = clean_text(company.get("name", "") if isinstance(company, dict) else company)
    loc = item.get("location") or {}
    data.location = clean_text(loc.get("fullLocation", "") if isinstance(loc, dict) else loc)
    data.posted_date = clean_text(item.get("releasedDate", ""))
    data.employment_type = clean_text((item.get("typeOfEmployment") or {}).get("label", "") if isinstance(item.get("typeOfEmployment"), dict) else item.get("typeOfEmployment", ""))
    sections = ((item.get("jobAd") or {}).get("sections") or {})
    data.description = clean_text(" ".join(str(sections.get(k, "")) for k in [
        "jobDescription", "qualifications", "additionalInformation",
    ]))
    data.notes.append("SmartRecruiters public Posting API")
    return data


def jobdata_from_workday(item, source_url):
    info = item.get("jobPostingInfo") or item
    data = supercrawler.JobData(source_url=source_url, status="jd_full_official_ats_api", confidence="high")
    data.title = clean_text(info.get("title", ""))
    data.company = clean_text(info.get("company", ""))
    data.location = clean_text(info.get("location", "") or info.get("jobRequisitionLocation", ""))
    data.posted_date = clean_text(info.get("postedOn", "") or info.get("startDate", ""))
    data.employment_type = clean_text(info.get("timeType", ""))
    data.description = clean_text(info.get("jobDescription", ""))
    data.notes.append("Workday public CXS job endpoint")
    return data


def try_ats_api(url, row, fetcher):
    api_builders = [
        ("greenhouse", greenhouse_api_url, jobdata_from_greenhouse),
        ("lever", lever_api_url, jobdata_from_lever),
        ("smartrecruiters", smartrecruiters_api_url, jobdata_from_smartrecruiters),
        ("workday", workday_api_url, jobdata_from_workday),
    ]
    for name, builder, converter in api_builders:
        api_url = builder(url)
        if not api_url:
            continue
        item, status = fetch_json(fetcher, api_url)
        if not isinstance(item, dict):
            data = supercrawler.JobData(source_url=api_url, status="jd_unavailable_fetch_error", confidence="none", fetch_status=status)
            data.notes.append(f"{name} API unavailable")
            return data
        data = converter(item, api_url)
        data.fetch_status = status
        data = supercrawler.evaluate_match(row, data)
        if len(data.description) < supercrawler.MIN_FULL_TEXT_CHARS:
            data.status = "jd_partial_summary_only"
            data.confidence = "low"
            data.notes.append("ATS API returned short description")
        return data
    return None


def relabel_page_status(data, kind):
    if data.status == "jd_full_structured":
        data.status = "jd_full_official_jsonld" if kind != "aggregator" else "jd_full_aggregator_match"
        data.confidence = "high" if kind != "aggregator" else "medium"
    elif data.status == "jd_full_page_text":
        data.status = "jd_full_official_page_text" if kind != "aggregator" else "jd_full_aggregator_match"
        data.confidence = "medium"
    return data


def try_page(url, row, fetcher):
    content, status = fetcher.fetch(url)
    if content is None:
        data = supercrawler.JobData(source_url=url, status="jd_unavailable_robots_blocked" if "robots" in status else "jd_unavailable_fetch_error", confidence="none", fetch_status=status)
        data.notes.append(status)
        return data
    data = supercrawler.data_from_html(url, content)
    data.fetch_status = status
    data = relabel_page_status(data, source_kind(url, row))
    data = supercrawler.evaluate_match(row, data)
    return data


def result_score(result):
    data = result.data
    weights = {
        "jd_full_input": 100,
        "jd_full_official_ats_api": 98,
        "jd_full_official_jsonld": 94,
        "jd_full_official_page_text": 82,
        "jd_full_aggregator_match": 62,
        "jd_partial_summary_only": 35,
        "jd_partial_possible_mismatch": 20,
        "jd_unavailable_robots_blocked": 8,
        "jd_unavailable_fetch_error": 6,
        "jd_unavailable_no_description": 5,
        "jd_unavailable_no_candidates": 3,
    }.get(data.status, 0)
    return weights + min(len(data.description) // 500, 12)


def choose_better(current, candidate):
    if current is None:
        return candidate
    return candidate if result_score(candidate) > result_score(current) else current


def resolve_row(row, fetcher, use_search=True, include_aggregators=False, max_queries=8, max_results=12, search_provider="auto"):
    existing_text = clean_text(row.get("job_text", ""))
    if len(existing_text) >= supercrawler.MIN_FULL_TEXT_CHARS:
        data = supercrawler.JobData(
            source_url=row.get("job_url", ""),
            status="jd_full_input",
            confidence="high",
            description=existing_text,
            notes=["Input row already contains full-length JD text"],
            fetch_status="not fetched",
        )
        return ResolvedJob(data=data, stage="input", source_kind="input")

    candidate_urls = []
    attempted = []
    rejected = []
    search_queries = []

    direct_candidates = supercrawler.canonical_candidate_urls(row, enable_search=False)
    candidate_urls.extend(direct_candidates)
    best = None

    for url in direct_candidates:
        kind = source_kind(url, row)
        attempted.append(url)
        data = None
        if kind == "official_ats":
            data = try_ats_api(url, row, fetcher)
            if data:
                attempted.append(data.source_url)
        if data is None:
            data = try_page(url, row, fetcher)
        result = ResolvedJob(data=data, stage="direct", source_kind=kind, attempted_urls=list(attempted), rejected_urls=list(rejected))
        best = choose_better(best, result)
        if best and best.data.status in {"jd_full_official_ats_api", "jd_full_official_jsonld"}:
            best.candidate_urls = unique(candidate_urls)
            return best

    if use_search:
        queries, search_candidates, search_attempted, search_rejected = discover_candidates(
            row,
            fetcher,
            max_queries=max_queries,
            max_results=max_results,
            include_aggregators=include_aggregators,
            search_provider=search_provider,
        )
        search_queries.extend(queries)
        attempted.extend(search_attempted)
        rejected.extend(search_rejected)
        for url in search_candidates:
            if url in candidate_urls:
                continue
            candidate_urls.append(url)
            kind = source_kind(url, row)
            attempted.append(url)
            data = None
            if kind == "official_ats":
                data = try_ats_api(url, row, fetcher)
                if data:
                    attempted.append(data.source_url)
            if data is None or data.status.startswith("jd_unavailable"):
                page_data = try_page(url, row, fetcher)
                data = page_data if data is None or result_score(ResolvedJob(page_data)) > result_score(ResolvedJob(data)) else data
            result = ResolvedJob(
                data=data,
                stage="search",
                source_kind=kind,
                search_queries=list(search_queries),
                candidate_urls=unique(candidate_urls),
                attempted_urls=list(attempted),
                rejected_urls=list(rejected),
            )
            best = choose_better(best, result)
            if best and best.data.status in {"jd_full_official_ats_api", "jd_full_official_jsonld"}:
                break

    if best is None:
        data = supercrawler.JobData(status="jd_unavailable_no_candidates", confidence="none")
        data.notes.append("No direct or search candidates produced a usable JD")
        best = ResolvedJob(data=data, stage="unresolved")
    best.search_queries = list(search_queries)
    best.candidate_urls = unique(candidate_urls)
    best.attempted_urls = list(attempted)
    best.rejected_urls = list(rejected)
    if best.data.status not in FULL_STATUSES and best.data.confidence == "none":
        best.data.notes.append("manual JD capture may be needed")
    return best


def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit(f"[!] No header row found in {path}.")
        return [{k: clean_text(v) for k, v in row.items()} for row in reader]


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def output_fieldnames(input_rows):
    seen = []
    for col in BASE_COLUMNS:
        if col not in seen:
            seen.append(col)
    for row in input_rows:
        for col in row:
            if col not in seen and col not in ENRICH_COLUMNS and col not in RESOLVER_COLUMNS:
                seen.append(col)
    return seen + [c for c in ENRICH_COLUMNS + RESOLVER_COLUMNS if c not in seen]


def apply_result(row, result):
    data = result.data
    out = dict(row)
    if data.description and len(data.description) > len(clean_text(out.get("job_text", ""))):
        out["job_text"] = data.description
    out.update({
        "jd_source_url": data.source_url,
        "jd_enrichment_status": data.status,
        "jd_confidence": data.confidence,
        "jd_confidence_notes": "; ".join(data.notes),
        "jd_extracted_title": data.title,
        "jd_extracted_company": data.company,
        "jd_extracted_location": data.location,
        "jd_posted_date": data.posted_date,
        "jd_employment_type": data.employment_type,
        "jd_salary": data.salary,
        "jd_fetch_status": data.fetch_status,
        "jd_resolver_stage": result.stage,
        "jd_resolver_source_kind": result.source_kind,
        "jd_search_queries": " | ".join(result.search_queries),
        "jd_candidate_urls": " | ".join(result.candidate_urls),
        "jd_attempted_urls": " | ".join(result.attempted_urls),
        "jd_rejected_urls": " | ".join(result.rejected_urls),
    })
    return out


def resolve(input_path, out_path, review_path, respect_robots=True, use_search=True, include_aggregators=False, max_queries=8, max_results=12, search_provider="auto"):
    rows = read_rows(input_path)
    fetcher = supercrawler.SafeFetcher(respect_robots=respect_robots)
    enriched = []
    review = []
    statuses = {}
    for idx, row in enumerate(rows, start=1):
        result = resolve_row(
            row,
            fetcher,
            use_search=use_search,
            include_aggregators=include_aggregators,
            max_queries=max_queries,
            max_results=max_results,
            search_provider=search_provider,
        )
        enriched_row = apply_result(row, result)
        enriched.append(enriched_row)
        review.append(enriched_row)
        statuses[result.data.status] = statuses.get(result.data.status, 0) + 1
        print(f"[*] {idx}/{len(rows)} {row.get('company', '')} | {row.get('role_title', '')}: {result.data.status}")
        time.sleep(0.05)

    write_csv(out_path, enriched, output_fieldnames(enriched))
    write_csv(review_path, review, REVIEW_COLUMNS)
    return {"rows": len(rows), "statuses": dict(sorted(statuses.items()))}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Resolve full JDs from alerts via official/ATS public sources."
    )
    parser.add_argument("--input", default="job_alerts_raw.csv", help="Raw Gmail/LinkedIn/SEEK alert CSV")
    parser.add_argument("--out", default="job_posts_enriched.csv", help="Enriched CSV to pass into prepare_job_posts.py")
    parser.add_argument("--review", default="jd_resolution_review.csv", help="Resolver provenance and confidence CSV")
    parser.add_argument("--no-search", action="store_true", help="Only try direct URLs; skip public search")
    parser.add_argument("--include-aggregators", action="store_true", help="Allow non-official aggregator result pages as lower-confidence JD sources")
    parser.add_argument("--max-queries", type=int, default=8, help="Search queries per row")
    parser.add_argument("--max-results", type=int, default=12, help="Candidate result URLs per row")
    parser.add_argument(
        "--search-provider",
        default="auto",
        choices=sorted(SEARCH_PROVIDERS),
        help="Search provider for official/ATS discovery. auto uses BRAVE_SEARCH_API_KEY, GOOGLE_CSE_API_KEY+GOOGLE_CSE_ID, BING_SEARCH_API_KEY, or SERPAPI_API_KEY. duckduckgo_html is diagnostic and may be robots-disallowed.",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Diagnostic only: fetch even when robots.txt disallows it. Do not use for the job pipeline.",
    )
    args = parser.parse_args(argv)
    summary = resolve(
        Path(args.input),
        Path(args.out),
        Path(args.review),
        respect_robots=not args.ignore_robots,
        use_search=not args.no_search,
        include_aggregators=args.include_aggregators,
        max_queries=args.max_queries,
        max_results=args.max_results,
        search_provider=args.search_provider,
    )
    print(f"[*] Rows processed: {summary['rows']}")
    print("[*] JD resolution statuses: " + ", ".join(
        f"{k}:{v}" for k, v in summary["statuses"].items()
    ))
    print(f"[*] Wrote {args.out}")
    print(f"[*] Review: {args.review}")


if __name__ == "__main__":
    main()
