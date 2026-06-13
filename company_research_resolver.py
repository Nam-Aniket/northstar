"""Resolve official-source company research for outreach personalization.

Pipeline position:
final.csv / people.csv / companies.txt -> company_research_resolver.py -> company_research.csv

Standard library only. Read-only HTTP/file fetching. No login, cookies, browser
automation, anti-bot bypass, or third-party claims. robots.txt is respected by
default for HTTP(S).
"""

import argparse
import csv
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date

import jd_resolver
import supercrawler


BASE_COLUMNS = [
    "company_domain",
    "official_update",
    "official_update_url",
    "official_update_date",
    "data_angle",
    "talent_angle",
    "checked_on",
]

PROVENANCE_COLUMNS = [
    "research_status",
    "confidence",
    "source_kind",
    "evidence_text",
    "candidate_urls",
    "attempted_urls",
    "rejected_urls",
    "fetch_status",
]

OUTPUT_COLUMNS = BASE_COLUMNS + PROVENANCE_COLUMNS

DATA_TERMS = {
    "ai",
    "artificial intelligence",
    "analytics",
    "automation",
    "cloud",
    "dashboard",
    "data",
    "digital",
    "insight",
    "machine learning",
    "operations",
    "platform",
    "power bi",
    "reporting",
    "sql",
    "technology",
    "transformation",
}

TALENT_TERMS = {
    "career",
    "careers",
    "expansion",
    "graduate",
    "growing",
    "growth",
    "hiring",
    "intern",
    "join",
    "opening",
    "people",
    "recruiting",
    "recruitment",
    "role",
    "team",
    "vacancy",
}

OFFICIAL_PATHS = [
    "/",
    "/careers",
    "/career",
    "/jobs",
    "/join-us",
    "/work-with-us",
    "/news",
    "/newsroom",
    "/media",
    "/press",
    "/blog",
    "/about",
]

PRIORITY_PATH_TERMS = {
    "about",
    "ai",
    "analytics",
    "blog",
    "career",
    "careers",
    "data",
    "digital",
    "insights",
    "jobs",
    "media",
    "news",
    "newsroom",
    "platform",
    "press",
    "technology",
}


@dataclass
class PageEvidence:
    url: str
    source_kind: str
    title: str = ""
    date: str = ""
    data_angle: str = ""
    talent_angle: str = ""
    official_update: str = ""
    evidence_text: str = ""
    confidence: str = "none"
    status: str = "research_unavailable"
    fetch_status: str = ""
    score: int = 0
    notes: list = field(default_factory=list)


@dataclass
class ResearchResult:
    company_domain: str
    official_update: str = ""
    official_update_url: str = ""
    official_update_date: str = ""
    data_angle: str = ""
    talent_angle: str = ""
    checked_on: str = ""
    research_status: str = "research_unavailable"
    confidence: str = "none"
    source_kind: str = ""
    evidence_text: str = ""
    candidate_urls: list = field(default_factory=list)
    attempted_urls: list = field(default_factory=list)
    rejected_urls: list = field(default_factory=list)
    fetch_status: str = ""


def clean_text(value):
    return supercrawler.clean_text(value)


def lower_ascii(value):
    return supercrawler.lower_ascii(value)


def unique(values):
    return supercrawler.unique(values)


def normalize_target(value):
    value = clean_text(value)
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://") or value.startswith("file://"):
        return supercrawler.normalize_url(value)
    if " " not in value and "." in value:
        return value.lower().strip("/")
    return value


def company_domain_from_row(row):
    for key in ("company_domain", "company", "company_name", "domain"):
        value = clean_text(row.get(key, ""))
        if value and "." in value and " " not in value:
            return normalize_target(value)
    return clean_text(row.get("company_name") or row.get("company") or "")


def read_companies(path):
    values = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                values.append(normalize_target(line))
    return [v for v in dict.fromkeys(values) if v]


def read_companies_from_contacts(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit(f"[!] No header row found in {path}.")
        values = [company_domain_from_row(row) for row in reader]
    return [v for v in dict.fromkeys(values) if v]


def is_domain(value):
    return bool(
        value
        and " " not in value
        and "." in value
        and not value.startswith("file://")
        and not value.startswith("http://")
        and not value.startswith("https://")
    )


def target_domain(value):
    value = normalize_target(value)
    if value.startswith("http://") or value.startswith("https://"):
        return supercrawler.url_host(value)
    if is_domain(value):
        return value
    return ""


def base_urls_for_domain(domain_or_url):
    value = normalize_target(domain_or_url)
    if value.startswith("file://"):
        return [value]
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urllib.parse.urlsplit(value)
        if parsed.path not in {"", "/"}:
            return [value]
        host = parsed.netloc
        scheme = parsed.scheme
    elif is_domain(value):
        host = value
        scheme = "https"
    else:
        return []
    return [
        urllib.parse.urlunsplit((scheme, host, path, "", ""))
        for path in OFFICIAL_PATHS
    ]


def source_kind(url):
    if url.startswith("file://"):
        return "local_fixture"
    path = urllib.parse.urlsplit(url).path.lower()
    if any(term in path for term in ["career", "jobs", "join-us", "work-with-us", "vacanc"]):
        return "official_careers"
    if any(term in path for term in ["news", "newsroom", "press", "media", "blog"]):
        return "official_news"
    if "about" in path:
        return "official_about"
    return "official_company"


def host_matches_target(url, target):
    if target.startswith("file://"):
        return url.startswith("file://")
    domain = target_domain(target)
    if not domain:
        return False
    return jd_resolver.host_matches_domain(supercrawler.url_host(url), domain)


def should_expand_link(url, target):
    if not host_matches_target(url, target):
        return False
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lower()
    tokens = set(re.findall(r"[a-z0-9]+", path))
    return bool(tokens & PRIORITY_PATH_TERMS)


def discover_from_search(target, search_provider="auto", max_results=8):
    domain = target_domain(target)
    if domain:
        queries = [
            f"site:{domain} careers OR jobs",
            f"site:{domain} news OR newsroom OR press",
            f"site:{domain} data analytics AI digital platform",
        ]
    else:
        quoted = jd_resolver.query_quote(target)
        queries = [
            f"{quoted} official careers",
            f"{quoted} official newsroom",
            f"{quoted} data analytics AI digital platform",
        ]
    candidates = []
    attempted = []
    rejected = []
    provider = jd_resolver.select_search_provider(search_provider)
    for query in queries:
        attempted.append(f"{provider}:{query}")
        links, status = jd_resolver.search_api_links(query, provider)
        if not links:
            rejected.append(f"{provider}:{query} [{status}]")
            continue
        for link in links:
            link = supercrawler.normalize_url(link)
            if not link:
                continue
            if domain and not host_matches_target(link, target):
                rejected.append(f"{link} [not official target domain]")
                continue
            if not domain:
                kind = jd_resolver.source_kind(link, {"company": target, "company_domain": ""})
                if kind not in {"official_company", "possible_company"}:
                    rejected.append(f"{link} [not official-looking result]")
                    continue
            candidates.append(link)
        if len(unique(candidates)) >= max_results:
            break
    return unique(candidates)[:max_results], attempted, rejected


def seed_candidates(target, use_search=True, search_provider="auto", max_results=8):
    target = normalize_target(target)
    candidates = base_urls_for_domain(target)
    attempted = []
    rejected = []
    if use_search:
        search_candidates, search_attempted, search_rejected = discover_from_search(
            target,
            search_provider=search_provider,
            max_results=max_results,
        )
        candidates.extend(search_candidates)
        attempted.extend(search_attempted)
        rejected.extend(search_rejected)
    return unique(candidates), attempted, rejected


def extract_date(parser, text):
    for key in [
        "article:published_time",
        "article:modified_time",
        "og:updated_time",
        "date",
        "publishdate",
        "pubdate",
    ]:
        if parser.meta.get(key):
            return clean_text(parser.meta[key])[:40]
    patterns = [
        r"\b20\d{2}-\d{2}-\d{2}\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+20\d{2}\b",
        r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return clean_text(match.group(0))
    return ""


def sentence_windows(text):
    text = clean_text(text)
    if not text:
        return []
    pieces = re.split(r"(?<=[.!?])\s+|\s+[|•]\s+", text)
    pieces = [clean_text(p) for p in pieces if len(clean_text(p)) >= 35]
    windows = []
    for idx, piece in enumerate(pieces):
        joined = clean_text(" ".join(pieces[idx:idx + 2]))
        if joined:
            windows.append(joined)
    if not windows and text:
        windows.append(text[:320])
    return windows


def term_score(text, terms):
    haystack = lower_ascii(text)
    score = 0
    for term in terms:
        if " " in term:
            if term in haystack:
                score += 2
        elif re.search(rf"\b{re.escape(term)}\b", haystack):
            score += 1
    return score


def best_angle(windows, terms):
    best = ""
    best_score = 0
    for window in windows:
        score = term_score(window, terms)
        if score > best_score:
            best = window
            best_score = score
    return clean_text(best[:320]), best_score


def parse_page(url, content):
    parser = supercrawler.JobHTMLParser()
    parser.feed(content)
    body = parser.body_text
    meta_description = (
        parser.meta.get("description")
        or parser.meta.get("og:description")
        or parser.meta.get("twitter:description")
        or ""
    )
    text = clean_text(" ".join(v for v in [parser.title, meta_description, body] if v))
    windows = sentence_windows(text)
    data_angle, data_score = best_angle(windows, DATA_TERMS)
    talent_angle, talent_score = best_angle(windows, TALENT_TERMS)
    scored = sorted(
        windows,
        key=lambda w: term_score(w, DATA_TERMS) * 2 + term_score(w, TALENT_TERMS),
        reverse=True,
    )
    update = clean_text((scored[0] if scored else text)[:320])
    score = data_score * 4 + talent_score * 3
    if source_kind(url) == "official_news":
        score += 8
    elif source_kind(url) == "official_careers":
        score += 6
    elif source_kind(url) == "official_about":
        score += 3
    status = "research_found" if score >= 5 and update else "research_weak"
    confidence = "high" if score >= 16 else "medium" if score >= 8 else "low" if update else "none"
    return PageEvidence(
        url=url,
        source_kind=source_kind(url),
        title=parser.title,
        date=extract_date(parser, text),
        data_angle=data_angle,
        talent_angle=talent_angle,
        official_update=update,
        evidence_text=update,
        confidence=confidence,
        status=status,
        score=score,
        notes=[f"data_score={data_score}", f"talent_score={talent_score}"],
    ), parser.links


def choose_better(current, candidate):
    if current is None:
        return candidate
    rank = {"high": 3, "medium": 2, "low": 1, "none": 0}
    current_key = (rank.get(current.confidence, 0), current.score, len(current.official_update))
    candidate_key = (rank.get(candidate.confidence, 0), candidate.score, len(candidate.official_update))
    return candidate if candidate_key > current_key else current


def research_company(target, fetcher, use_search=True, search_provider="auto", max_pages=10, checked_on=None):
    target = normalize_target(target)
    checked_on = checked_on or date.today().isoformat()
    candidates, attempted, rejected = seed_candidates(
        target,
        use_search=use_search,
        search_provider=search_provider,
        max_results=max_pages,
    )
    best = None
    idx = 0
    while idx < len(candidates) and len(attempted) < max_pages + 8:
        url = candidates[idx]
        idx += 1
        if not (target.startswith("file://") or host_matches_target(url, target) or not is_domain(target)):
            rejected.append(f"{url} [not official target domain]")
            continue
        attempted.append(url)
        content, fetch_status = fetcher.fetch(url)
        if content is None:
            rejected.append(f"{url} [{fetch_status}]")
            continue
        page, links = parse_page(url, content)
        page.fetch_status = fetch_status
        best = choose_better(best, page)
        if page.confidence == "high" and page.data_angle:
            break
        for href in links:
            link = supercrawler.absolute_url(url, href)
            if link and link not in candidates and should_expand_link(link, target):
                candidates.append(link)

    if best is None:
        return ResearchResult(
            company_domain=target,
            checked_on=checked_on,
            research_status="research_unavailable",
            confidence="none",
            candidate_urls=candidates,
            attempted_urls=attempted,
            rejected_urls=rejected or ["No official candidate page produced usable evidence"],
        )

    return ResearchResult(
        company_domain=target,
        official_update=best.official_update,
        official_update_url=best.url,
        official_update_date=best.date,
        data_angle=best.data_angle,
        talent_angle=best.talent_angle,
        checked_on=checked_on,
        research_status=best.status,
        confidence=best.confidence,
        source_kind=best.source_kind,
        evidence_text=best.evidence_text,
        candidate_urls=unique(candidates),
        attempted_urls=attempted,
        rejected_urls=rejected,
        fetch_status=best.fetch_status,
    )


def result_to_row(result):
    return {
        "company_domain": result.company_domain,
        "official_update": result.official_update,
        "official_update_url": result.official_update_url,
        "official_update_date": result.official_update_date,
        "data_angle": result.data_angle,
        "talent_angle": result.talent_angle,
        "checked_on": result.checked_on,
        "research_status": result.research_status,
        "confidence": result.confidence,
        "source_kind": result.source_kind,
        "evidence_text": result.evidence_text,
        "candidate_urls": " | ".join(result.candidate_urls),
        "attempted_urls": " | ".join(result.attempted_urls),
        "rejected_urls": " | ".join(result.rejected_urls),
        "fetch_status": result.fetch_status,
    }


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def resolve(companies, out_path, respect_robots=True, use_search=True, search_provider="auto", max_pages=10):
    fetcher = supercrawler.SafeFetcher(respect_robots=respect_robots)
    rows = []
    statuses = {}
    for idx, company in enumerate(companies, start=1):
        result = research_company(
            company,
            fetcher,
            use_search=use_search,
            search_provider=search_provider,
            max_pages=max_pages,
        )
        rows.append(result_to_row(result))
        statuses[result.research_status] = statuses.get(result.research_status, 0) + 1
        print(f"[*] {idx}/{len(companies)} {company}: {result.research_status} ({result.confidence})")
        time.sleep(0.05)
    write_csv(out_path, rows)
    return {"companies": len(companies), "statuses": dict(sorted(statuses.items()))}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build official-source company research CSV for outreach personalization."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--companies", help="Text file with one company domain/name per line")
    source.add_argument("--contacts", help="People CSV; company_domain/company_name/company columns are deduped")
    parser.add_argument("--out", default="company_research.csv", help="Output CSV")
    parser.add_argument("--no-search", action="store_true", help="Only try direct official URLs; skip search APIs")
    parser.add_argument("--max-pages", type=int, default=10, help="Maximum pages to attempt per company")
    parser.add_argument(
        "--search-provider",
        default="auto",
        choices=sorted(jd_resolver.SEARCH_PROVIDERS),
        help="Search provider for official page discovery. auto uses configured supported search API keys.",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Diagnostic only. Default respects robots.txt.",
    )
    args = parser.parse_args(argv)

    if args.companies:
        companies = read_companies(args.companies)
    else:
        companies = read_companies_from_contacts(args.contacts)
    if not companies:
        sys.exit("[!] No companies found.")
    summary = resolve(
        companies,
        args.out,
        respect_robots=not args.ignore_robots,
        use_search=not args.no_search,
        search_provider=args.search_provider,
        max_pages=args.max_pages,
    )
    print(f"[OK] Wrote {args.out}: {summary}")


if __name__ == "__main__":
    main()
