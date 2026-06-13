"""Compatibility helpers for resolver modules that import `supercrawler`."""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field

from local_apify_crawler import SimpleHTMLExtractor, clean_text, unique
from superpowered_crawler_final import SuperpoweredCrawlerFinal


BASE_COLUMNS = [
    "company",
    "company_domain",
    "role_title",
    "location",
    "job_url",
    "job_text",
    "required_skills",
    "preferred_skills",
    "notes",
]

ENRICH_COLUMNS = [
    "jd_source_url",
    "jd_enrichment_status",
    "jd_confidence",
    "jd_confidence_notes",
    "jd_extracted_title",
    "jd_extracted_company",
    "jd_extracted_location",
    "jd_posted_date",
    "jd_employment_type",
    "jd_salary",
    "jd_fetch_status",
]

MIN_FULL_TEXT_CHARS = 350


@dataclass
class JobData:
    source_url: str = ""
    status: str = ""
    confidence: str = ""
    description: str = ""
    notes: list[str] = field(default_factory=list)
    title: str = ""
    company: str = ""
    location: str = ""
    posted_date: str = ""
    employment_type: str = ""
    salary: str = ""
    fetch_status: str = ""


class SafeFetcher:
    def __init__(self, respect_robots: bool = True):
        self.crawler = SuperpoweredCrawlerFinal(
            respect_robots=respect_robots,
            polite_delay=0.5,
            rps=0.75,
        )

    def fetch(self, url: str):
        result = self.crawler.fetch(url)
        if result.error or not result.text:
            status = result.error or str(result.status or "fetch failed")
            return None, status
        return result.text, str(result.status or "ok")


def canonical_candidate_urls(row: dict, enable_search: bool = False) -> list[str]:
    url = clean_text(row.get("job_url", ""))
    return [url] if url else []


def lower_ascii(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", clean_text(value))
    return "".join(ch for ch in normalized if ord(ch) < 128).lower()


def token_set(value: str) -> set:
    """Alphanumeric token set, used by jd_resolver for URL/title overlap scoring."""
    return set(re.findall(r"[a-z0-9]+", lower_ascii(value)))


def url_host(url: str) -> str:
    return urllib.parse.urlsplit(clean_text(url)).netloc.lower()


def extract_search_result_links(url: str, content: str) -> list[str]:
    links = []
    for href in re.findall(r'class="result__a" href="([^"]+)"', content or ""):
        href = href.replace("&amp;", "&")
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs:
            href = qs["uddg"][0]
        href = urllib.parse.unquote(href)
        if href.startswith("http"):
            links.append(href)
    return links


def _strip_html(html: str) -> str:
    return clean_text(re.sub(r"<[^>]+>", " ", html or ""))


def _first_jsonld_jobposting(parser: SimpleHTMLExtractor) -> dict:
    for block in parser.json_ld:
        try:
            payload = json.loads(block)
        except Exception:
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if any(str(t).lower() == "jobposting" for t in types if t):
                return item
    return {}


def data_from_html(url: str, content: str) -> JobData:
    parser = SimpleHTMLExtractor()
    try:
        parser.feed(content or "")
    except Exception:
        pass

    jobposting = _first_jsonld_jobposting(parser)
    description = clean_text(jobposting.get("description", "")) if jobposting else ""
    title = clean_text(jobposting.get("title", "")) if jobposting else ""
    company = ""
    location = ""
    posted_date = ""
    employment_type = ""
    salary = ""

    if jobposting:
        hiring_org = jobposting.get("hiringOrganization") or {}
        if isinstance(hiring_org, dict):
            company = clean_text(hiring_org.get("name", ""))
        job_loc = jobposting.get("jobLocation")
        if isinstance(job_loc, dict):
            address = job_loc.get("address") or {}
            if isinstance(address, dict):
                location = clean_text(", ".join(
                    part for part in [
                        address.get("addressLocality", ""),
                        address.get("addressRegion", ""),
                        address.get("addressCountry", ""),
                    ] if clean_text(part)
                ))
        posted_date = clean_text(jobposting.get("datePosted", ""))
        employment_type = clean_text(jobposting.get("employmentType", ""))
        base_salary = jobposting.get("baseSalary") or {}
        if isinstance(base_salary, dict):
            salary = clean_text(json.dumps(base_salary))

    visible_text = clean_text(parser.visible_text)
    if len(description) < MIN_FULL_TEXT_CHARS:
        description = visible_text

    if len(description) >= MIN_FULL_TEXT_CHARS:
        status = "jd_full_structured" if jobposting else "jd_full_page_text"
        confidence = "high" if jobposting else "medium"
    else:
        status = "jd_unavailable_no_description"
        confidence = "none"

    return JobData(
        source_url=url,
        status=status,
        confidence=confidence,
        description=description,
        notes=[],
        title=title or clean_text(parser.title),
        company=company,
        location=location,
        posted_date=posted_date,
        employment_type=employment_type,
        salary=_strip_html(salary),
    )


def evaluate_match(row: dict, data: JobData) -> JobData:
    wanted_title = clean_text(row.get("role_title", "")).lower()
    wanted_company = clean_text(row.get("company", "")).lower()
    found_title = clean_text(data.title).lower()
    found_company = clean_text(data.company).lower()

    if wanted_title and found_title and wanted_title not in found_title and found_title not in wanted_title:
        data.notes.append("title mismatch: verify JD manually")
    if wanted_company and found_company and wanted_company not in found_company and found_company not in wanted_company:
        data.notes.append("company mismatch: verify JD manually")
    if data.status.startswith("jd_full") and len(data.description) < MIN_FULL_TEXT_CHARS:
        data.status = "jd_partial_summary_only"
        data.confidence = "low"
    return data
