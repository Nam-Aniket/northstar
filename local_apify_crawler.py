#!/usr/bin/env python3
"""
Local Apify-style crawler for company, contact, LinkedIn, and jobs enrichment.

The crawler is intentionally conservative:
- it validates domains against company tokens and page evidence;
- it crawls focused pages only;
- it records evidence and rejected domain reasons;
- it leaves optional fields blank rather than guessing.

It uses only the Python standard library so it can run in this workspace without
extra installs.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import hashlib
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request


USER_AGENT = "Mozilla/5.0 (compatible; LocalCompanyCrawler/1.0)"

EMAIL_RE = re.compile(
    r"(?i)(?<![A-Z0-9._%+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![A-Z0-9._%+-])"
)
PHONE_RE = re.compile(
    r"(?:(?:\+?61|0)[\s().-]*)?(?:[2378][\s().-]*\d{4}[\s().-]*\d{4}|4\d{2}[\s().-]*\d{3}[\s().-]*\d{3})"
)

CONTACT_TOKENS = [
    "contact",
    "enquiry",
    "enquiries",
    "support",
    "customer-service",
    "customer",
    "locations",
    "store-locator",
    "stores",
]
ABOUT_TOKENS = ["about", "company", "who-we-are", "our-story", "corporate"]
LEADERSHIP_TOKENS = ["leadership", "board", "governance", "team", "executive", "our-people"]
CAREERS_TOKENS = ["careers", "jobs", "join-us", "work-with-us", "vacancies", "employment"]

BAD_EMAIL_TOKENS = [
    "sentry",
    "wixpress",
    "cloudflare",
    "localhost",
    "invalid",
    "example",
    "test.",
]
BAD_EMAIL_PREFIXES = ["noreply@", "no-reply@", "donotreply@", "do-not-reply@", "name@", "email@"]
BAD_EMAIL_SUFFIXES = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".css", ".js"]

NOISE_DOMAINS = [
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "x.com",
    "twitter.com",
    "abr.business.gov.au",
    "acnc.gov.au",
    "wgea.gov.au",
    "abn-lookup.com",
    "zoominfo.com",
    "rocketreach.co",
    "signalhire.com",
    "dnb.com",
    "crunchbase.com",
    "glassdoor.com",
    "seek.com.au",
    "indeed.com",
]

LEGAL_STOPWORDS = {
    "pty",
    "proprietary",
    "limited",
    "ltd",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "company",
    "co",
    "group",
    "holdings",
    "holding",
    "australia",
    "australian",
    "services",
    "service",
    "operations",
    "management",
    "trust",
    "trustee",
    "trustees",
    "unit",
    "the",
    "for",
    "and",
}


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_url(url: str) -> str:
    url = clean_text(url)
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if not re.match(r"(?i)^https?://", url):
        url = "https://" + url
    return url


def domain_of(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host.split(":")[0]


def root_domain(domain: str) -> str:
    domain = domain.lower().strip(".")
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in {"com", "org", "net", "edu", "gov"} and parts[-1] == "au":
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return domain


def same_site(a: str, b: str) -> bool:
    da = root_domain(domain_of(a) or a)
    db = root_domain(domain_of(b) or b)
    return bool(da and db and da == db)


def url_join(base: str, href: str) -> str:
    href = href.strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return ""
    return urllib.parse.urldefrag(urllib.parse.urljoin(base, href))[0]


def significant_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", clean_text(text).lower())
    return [token for token in tokens if len(token) >= 4 and token not in LEGAL_STOPWORDS]


def compact_brand(text: str) -> str:
    return "".join(significant_tokens(text))


def unique(values, limit: int | None = None) -> list[str]:
    seen = []
    for value in values:
        value = clean_text(value)
        if value and value not in seen:
            seen.append(value)
            if limit and len(seen) >= limit:
                break
    return seen


def is_noise_domain(url_or_domain: str) -> bool:
    domain = domain_of(url_or_domain) or url_or_domain.lower()
    return any(noise in domain for noise in NOISE_DOMAINS)


def is_bad_email(email: str) -> bool:
    e = email.lower()
    if any(token in e for token in BAD_EMAIL_TOKENS):
        return True
    if any(e.startswith(prefix) for prefix in BAD_EMAIL_PREFIXES):
        return True
    if any(e.endswith(suffix) for suffix in BAD_EMAIL_SUFFIXES):
        return True
    return False


def email_matches_site(email: str, site_url: str) -> bool:
    email_domain = email.split("@")[-1].lower()
    site_domain = root_domain(domain_of(site_url))
    if not email_domain or not site_domain:
        return False
    email_root = root_domain(email_domain)
    return email_root == site_domain or email_domain.endswith("." + site_domain)


def normalize_phone(phone: str) -> str:
    phone = re.sub(r"\s+", " ", phone).strip()
    phone = re.sub(r"(?<=\d)[().-]+(?=\d)", " ", phone)
    return phone


class SimpleHTMLExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.meta: dict[str, str] = {}
        self.title = ""
        self.text_parts: list[str] = []
        self._current_href = ""
        self._current_link_text: list[str] = []
        self._in_title = False
        self.json_ld: list[str] = []
        self._in_json_ld = False
        self._json_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "a":
            self._current_href = attrs_dict.get("href", "")
            self._current_link_text = []
        elif tag == "meta":
            key = (attrs_dict.get("name") or attrs_dict.get("property") or "").lower()
            val = attrs_dict.get("content", "")
            if key and val:
                self.meta[key] = val
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and attrs_dict.get("type", "").lower() == "application/ld+json":
            self._in_json_ld = True
            self._json_parts = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "a" and self._current_href:
            self.links.append((self._current_href, clean_text(" ".join(self._current_link_text))))
            self._current_href = ""
            self._current_link_text = []
        elif tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_json_ld:
            self.json_ld.append("".join(self._json_parts))
            self._in_json_ld = False
            self._json_parts = []

    def handle_data(self, data):
        if not data:
            return
        if self._current_href:
            self._current_link_text.append(data)
        if self._in_title:
            self.title += data
        if self._in_json_ld:
            self._json_parts.append(data)
        if not self._in_json_ld:
            stripped = clean_text(data)
            if stripped:
                self.text_parts.append(stripped)

    @property
    def visible_text(self) -> str:
        return clean_text(" ".join(self.text_parts))[:200_000]


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int | str = ""
    content_type: str = ""
    text: str = ""
    error: str = ""
    elapsed_ms: int = 0


@dataclass
class DomainCandidate:
    url: str
    source: str
    candidate_name: str = ""
    confidence_hint: int = 0


@dataclass
class PageEvidence:
    url: str
    title: str = ""
    meta_description: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    linkedin: list[str] = field(default_factory=list)
    careers: list[str] = field(default_factory=list)
    contact_links: list[str] = field(default_factory=list)
    about_links: list[str] = field(default_factory=list)
    leadership_links: list[str] = field(default_factory=list)
    job_links: list[str] = field(default_factory=list)


class LocalCrawler:
    def __init__(
        self,
        *,
        country_hint: str = "Australia",
        require_au_domain: bool = False,
        max_pages: int = 8,
        timeout: int = 12,
        use_search: bool = False,
        clearbit: bool = True,
        polite_delay: float = 0.0,
    ):
        self.country_hint = country_hint
        self.require_au_domain = require_au_domain
        self.max_pages = max_pages
        self.timeout = timeout
        self.use_search = use_search
        self.clearbit = clearbit
        self.polite_delay = polite_delay

    def fetch(self, url: str, max_bytes: int = 1_500_000) -> FetchResult:
        started = time.time()
        result = FetchResult(url=url)
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.8,text/plain;q=0.7,*/*;q=0.4",
                },
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                data = response.read(max_bytes)
                charset = response.headers.get_content_charset() or "utf-8"
                result.final_url = response.geturl()
                result.status = response.status
                result.content_type = response.headers.get("content-type", "")
                result.text = data.decode(charset, "ignore")
        except urllib.error.HTTPError as exc:
            result.status = exc.code
            result.error = f"HTTPError: {exc.code}"
            result.final_url = getattr(exc, "url", url)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        result.elapsed_ms = int((time.time() - started) * 1000)
        if self.polite_delay:
            time.sleep(self.polite_delay)
        return result

    def parse_page(self, fetch: FetchResult) -> PageEvidence:
        page = PageEvidence(url=fetch.final_url or fetch.url)
        if not fetch.text:
            return page
        parser = SimpleHTMLExtractor()
        try:
            parser.feed(fetch.text)
        except Exception:
            pass
        page.title = clean_text(parser.title)
        page.meta_description = clean_text(
            parser.meta.get("description") or parser.meta.get("og:description") or parser.meta.get("twitter:description")
        )
        page.text = parser.visible_text
        page.emails = unique(email for email in EMAIL_RE.findall(fetch.text) if not is_bad_email(email))
        page.phones = unique(normalize_phone(phone) for phone in PHONE_RE.findall(page.text))

        links = []
        for href, label in parser.links:
            link = url_join(page.url, href)
            if not link:
                continue
            links.append((link, label))
        page.links = unique_link_pairs(links)

        for link, label in page.links:
            low = (link + " " + label).lower()
            if "linkedin.com/company" in low or "linkedin.com/school" in low:
                page.linkedin.append(link)
            if any(token in low for token in CONTACT_TOKENS):
                page.contact_links.append(link)
            if any(token in low for token in ABOUT_TOKENS):
                page.about_links.append(link)
            if any(token in low for token in LEADERSHIP_TOKENS):
                page.leadership_links.append(link)
            if any(token in low for token in CAREERS_TOKENS):
                page.careers.append(link)
            if any(token in low for token in ["job", "position", "vacancy", "greenhouse.io", "lever.co", "workdayjobs"]):
                page.job_links.append(link)

        page.linkedin = unique(page.linkedin, 10)
        page.contact_links = unique(page.contact_links, 20)
        page.about_links = unique(page.about_links, 20)
        page.leadership_links = unique(page.leadership_links, 20)
        page.careers = unique(page.careers, 20)
        page.job_links = unique(page.job_links, 20)
        return page

    def candidate_terms(self, company_name: str, extra_terms: list[str] | None = None) -> list[str]:
        terms = [company_name]
        compact = " ".join(significant_tokens(company_name))
        if compact:
            terms.append(compact)
        if extra_terms:
            terms.extend(extra_terms)
        return unique(terms, 6)

    def discover_candidates(
        self,
        company_name: str,
        *,
        website_hint: str = "",
        extra_terms: list[str] | None = None,
    ) -> list[DomainCandidate]:
        candidates: list[DomainCandidate] = []
        if website_hint:
            candidates.append(DomainCandidate(normalize_url(website_hint), "input_website", confidence_hint=40))

        terms = self.candidate_terms(company_name, extra_terms)
        if self.clearbit:
            for term in terms[:4]:
                candidates.extend(self.clearbit_candidates(term, company_name))

        candidates.extend(self.guessed_domain_candidates(company_name))

        if self.use_search:
            for term in terms[:2]:
                candidates.extend(self.duckduckgo_candidates(f'"{term}" {self.country_hint} official website', company_name))

        deduped: dict[str, DomainCandidate] = {}
        for candidate in candidates:
            if not candidate.url or is_noise_domain(candidate.url):
                continue
            key = root_domain(domain_of(candidate.url))
            if not key:
                continue
            if key not in deduped or candidate.confidence_hint > deduped[key].confidence_hint:
                deduped[key] = candidate
        return sorted(deduped.values(), key=lambda c: c.confidence_hint, reverse=True)

    def clearbit_candidates(self, query: str, company_name: str) -> list[DomainCandidate]:
        url = "https://autocomplete.clearbit.com/v1/companies/suggest?" + urllib.parse.urlencode({"query": query})
        fetch = self.fetch(url, max_bytes=250_000)
        if fetch.error or not fetch.text:
            return []
        try:
            data = json.loads(fetch.text)
        except Exception:
            return []
        candidates = []
        for item in data[:8]:
            domain = clean_text(item.get("domain"))
            candidate_name = clean_text(item.get("name"))
            if not domain:
                continue
            if self.require_au_domain and not domain.endswith(".au"):
                continue
            score = self.name_domain_score(company_name, candidate_name, domain)
            if score >= 25:
                candidates.append(
                    DomainCandidate(f"https://{domain}", "clearbit", candidate_name=candidate_name, confidence_hint=score)
                )
        return candidates

    def guessed_domain_candidates(self, company_name: str) -> list[DomainCandidate]:
        tokens = significant_tokens(company_name)
        if not tokens:
            return []
        joined = "".join(tokens[:3])
        hyphen = "-".join(tokens[:3])
        candidates = []
        suffixes = [".com.au", ".com", ".org.au", ".net.au"]
        for stem in unique([joined, hyphen, tokens[0]], 4):
            for suffix in suffixes:
                if self.require_au_domain and not suffix.endswith(".au"):
                    continue
                candidates.append(DomainCandidate(f"https://www.{stem}{suffix}", "guessed_domain", confidence_hint=10))
                candidates.append(DomainCandidate(f"https://{stem}{suffix}", "guessed_domain", confidence_hint=8))
        return candidates

    def duckduckgo_candidates(self, query: str, company_name: str) -> list[DomainCandidate]:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        fetch = self.fetch(url, max_bytes=800_000)
        if fetch.error or not fetch.text:
            return []
        candidates = []
        for href in re.findall(r'class="result__a" href="([^"]+)"', fetch.text):
            href = href.replace("&amp;", "&")
            parsed = urllib.parse.urlparse(href)
            qs = urllib.parse.parse_qs(parsed.query)
            if "uddg" in qs:
                href = qs["uddg"][0]
            href = urllib.parse.unquote(href)
            if not href.startswith("http") or is_noise_domain(href):
                continue
            if self.require_au_domain and not domain_of(href).endswith(".au"):
                continue
            score = self.name_domain_score(company_name, "", domain_of(href))
            candidates.append(DomainCandidate(href, "duckduckgo", confidence_hint=max(score, 12)))
        return candidates[:8]

    def name_domain_score(self, company_name: str, candidate_name: str, domain: str) -> int:
        tokens = significant_tokens(company_name)
        target = " ".join([candidate_name, domain]).lower()
        if not tokens:
            return 0
        matches = [token for token in tokens if token in target]
        score = 10 * len(matches)
        if compact_brand(company_name) and compact_brand(company_name) in re.sub(r"[^a-z0-9]", "", target):
            score += 30
        if domain.endswith(".au"):
            score += 8
        return min(score, 70)

    def validate_domain(self, company_name: str, candidate: DomainCandidate, fetch: FetchResult, page: PageEvidence) -> tuple[bool, int, str]:
        if fetch.error:
            return False, 0, fetch.error
        if not fetch.text:
            return False, 0, "empty response"
        if "text/html" not in fetch.content_type.lower() and "<html" not in fetch.text[:1000].lower():
            return False, 0, "not html"
        if is_noise_domain(fetch.final_url or candidate.url):
            return False, 0, "noise domain"
        domain = domain_of(fetch.final_url or candidate.url)
        if self.require_au_domain and not domain.endswith(".au"):
            return False, 0, "non-AU domain rejected"

        tokens = significant_tokens(company_name)
        evidence_text = " ".join([domain, page.title, page.meta_description, page.text[:20_000]]).lower()
        token_hits = [token for token in tokens if token in evidence_text]
        score = candidate.confidence_hint
        score += 15 * min(len(token_hits), 3)
        if self.country_hint and self.country_hint.lower() in evidence_text:
            score += 10
        if ".au" in domain:
            score += 8
        if page.emails:
            score += 5
        if page.linkedin:
            score += 5
        if page.contact_links:
            score += 5

        min_hits = 1 if len(tokens) <= 2 else 2
        if len(token_hits) < min_hits and candidate.source not in {"input_website"}:
            return False, score, f"insufficient company-token evidence: {token_hits}"
        if score < 45:
            return False, score, "low confidence score"
        return True, min(score, 100), "accepted"

    def crawl_company(
        self,
        company_name: str,
        *,
        row_id: str = "",
        website_hint: str = "",
        extra_terms: list[str] | None = None,
        mode: str = "all",
    ) -> dict:
        domain_candidates = self.discover_candidates(company_name, website_hint=website_hint, extra_terms=extra_terms)
        rejected = []
        accepted = None
        homepage_fetch = None
        homepage_page = None
        accepted_score = 0

        for candidate in domain_candidates[:12]:
            fetch = self.fetch(candidate.url)
            page = self.parse_page(fetch)
            ok, score, reason = self.validate_domain(company_name, candidate, fetch, page)
            rejected.append(
                {
                    "row_id": row_id,
                    "company_name": company_name,
                    "candidate_url": candidate.url,
                    "final_url": fetch.final_url,
                    "candidate_source": candidate.source,
                    "candidate_name": candidate.candidate_name,
                    "score": score,
                    "accepted": ok,
                    "reason": reason,
                }
            )
            if ok:
                accepted = candidate
                homepage_fetch = fetch
                homepage_page = page
                accepted_score = score
                break

        if not accepted or not homepage_page or not homepage_fetch:
            return self.empty_result(company_name, row_id, rejected)

        pages = [homepage_page]
        page_logs = [self.page_log(row_id, company_name, homepage_fetch, homepage_page)]
        crawl_queue = self.build_crawl_queue(homepage_page, homepage_fetch.final_url or accepted.url, mode)
        seen_urls = {homepage_page.url}
        for url in crawl_queue:
            if len(pages) >= self.max_pages:
                break
            if url in seen_urls or not same_site(url, homepage_page.url):
                continue
            seen_urls.add(url)
            fetch = self.fetch(url)
            page = self.parse_page(fetch)
            pages.append(page)
            page_logs.append(self.page_log(row_id, company_name, fetch, page))

        extracted = self.aggregate_evidence(company_name, row_id, homepage_page.url, accepted_score, pages, rejected, page_logs)
        return extracted

    def build_crawl_queue(self, homepage: PageEvidence, base_url: str, mode: str) -> list[str]:
        queue = []
        if mode in {"all", "contact", "company", "linkedin"}:
            queue.extend(homepage.contact_links)
            queue.extend(homepage.about_links)
            queue.extend(homepage.leadership_links)
        if mode in {"all", "jobs"}:
            queue.extend(homepage.careers)
            queue.extend(homepage.job_links)
        queue.extend(self.sitemap_urls(base_url))
        priority = []
        for url in unique(queue, 60):
            low = url.lower()
            weight = 0
            if any(token in low for token in CONTACT_TOKENS):
                weight += 30
            if any(token in low for token in CAREERS_TOKENS):
                weight += 20
            if any(token in low for token in ABOUT_TOKENS + LEADERSHIP_TOKENS):
                weight += 15
            priority.append((weight, url))
        return [url for _, url in sorted(priority, reverse=True)]

    def sitemap_urls(self, base_url: str) -> list[str]:
        parsed = urllib.parse.urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            return []
        sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
        fetch = self.fetch(sitemap_url, max_bytes=600_000)
        if fetch.error or not fetch.text:
            return []
        urls = re.findall(r"<loc>\s*([^<]+)\s*</loc>", fetch.text, flags=re.I)
        useful = []
        for url in urls:
            low = url.lower()
            if any(token in low for token in CONTACT_TOKENS + ABOUT_TOKENS + LEADERSHIP_TOKENS + CAREERS_TOKENS):
                useful.append(url)
        return useful[:30]

    def aggregate_evidence(
        self,
        company_name: str,
        row_id: str,
        website: str,
        confidence_score: int,
        pages: list[PageEvidence],
        rejected_domains: list[dict],
        page_logs: list[dict],
    ) -> dict:
        emails = []
        phones = []
        linkedin = []
        contact_pages = []
        careers_pages = []
        leadership_pages = []
        job_links = []
        evidence_rows = []

        for page in pages:
            page_emails = [email for email in page.emails if email_matches_site(email, website)]
            emails.extend(page_emails)
            phones.extend(page.phones)
            linkedin.extend(page.linkedin)
            contact_pages.extend(page.contact_links)
            careers_pages.extend(page.careers)
            leadership_pages.extend(page.leadership_links)
            job_links.extend(page.job_links)
            for email in page_emails:
                evidence_rows.append(self.evidence(row_id, company_name, "email", email, page.url, confidence_score))
            for phone in page.phones[:5]:
                evidence_rows.append(self.evidence(row_id, company_name, "phone", phone, page.url, confidence_score))
            for link in page.linkedin[:5]:
                evidence_rows.append(self.evidence(row_id, company_name, "linkedin_url", link, page.url, confidence_score))
            for link in page.careers[:5]:
                evidence_rows.append(self.evidence(row_id, company_name, "careers_url", link, page.url, confidence_score))

        emails = unique(emails, 10)
        phones = unique(phones, 10)
        linkedin = unique(linkedin, 8)
        contact_pages = unique([u for u in contact_pages if same_site(u, website)], 8)
        careers_pages = unique([u for u in careers_pages if same_site(u, website)], 8)
        leadership_pages = unique([u for u in leadership_pages if same_site(u, website)], 8)
        job_links = unique(job_links, 20)

        label = "High" if confidence_score >= 80 else "Medium" if confidence_score >= 60 else "Low"
        return {
            "row_id": row_id,
            "company_name": company_name,
            "accepted_website": website,
            "confidence_score": confidence_score,
            "confidence_label": label,
            "contact_emails": "; ".join(emails),
            "primary_email": emails[0] if emails else "",
            "phones": "; ".join(phones),
            "primary_phone": phones[0] if phones else "",
            "linkedin_urls": "; ".join(linkedin),
            "primary_linkedin": linkedin[0] if linkedin else "",
            "contact_pages": "; ".join(contact_pages),
            "primary_contact_page": contact_pages[0] if contact_pages else "",
            "careers_pages": "; ".join(careers_pages),
            "primary_careers_page": careers_pages[0] if careers_pages else "",
            "leadership_pages": "; ".join(leadership_pages),
            "primary_leadership_page": leadership_pages[0] if leadership_pages else "",
            "job_links": "; ".join(job_links),
            "job_link_count": len(job_links),
            "pages_crawled": len(pages),
            "evidence_rows": evidence_rows,
            "rejected_domains": rejected_domains,
            "page_logs": page_logs,
        }

    def evidence(self, row_id: str, company_name: str, field_name: str, value: str, source_url: str, confidence: int) -> dict:
        return {
            "row_id": row_id,
            "company_name": company_name,
            "field": field_name,
            "value": value,
            "source_url": source_url,
            "confidence_score": confidence,
        }

    def page_log(self, row_id: str, company_name: str, fetch: FetchResult, page: PageEvidence) -> dict:
        return {
            "row_id": row_id,
            "company_name": company_name,
            "url": fetch.url,
            "final_url": fetch.final_url,
            "status": fetch.status,
            "content_type": fetch.content_type,
            "error": fetch.error,
            "elapsed_ms": fetch.elapsed_ms,
            "title": page.title,
            "email_count": len(page.emails),
            "link_count": len(page.links),
        }

    def empty_result(self, company_name: str, row_id: str, rejected_domains: list[dict]) -> dict:
        return {
            "row_id": row_id,
            "company_name": company_name,
            "accepted_website": "",
            "confidence_score": 0,
            "confidence_label": "Rejected",
            "contact_emails": "",
            "primary_email": "",
            "phones": "",
            "primary_phone": "",
            "linkedin_urls": "",
            "primary_linkedin": "",
            "contact_pages": "",
            "primary_contact_page": "",
            "careers_pages": "",
            "primary_careers_page": "",
            "leadership_pages": "",
            "primary_leadership_page": "",
            "job_links": "",
            "job_link_count": 0,
            "pages_crawled": 0,
            "evidence_rows": [],
            "rejected_domains": rejected_domains,
            "page_logs": [],
        }


def unique_link_pairs(pairs: list[tuple[str, str]], limit: int | None = None) -> list[tuple[str, str]]:
    seen = []
    keys = set()
    for url, label in pairs:
        if url not in keys:
            seen.append((url, label))
            keys.add(url)
            if limit and len(seen) >= limit:
                break
    return seen


def row_hash(row: dict, name_col: str) -> str:
    key = clean_text(row.get(name_col)) or json.dumps(row, sort_keys=True)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not fieldnames:
        keys = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        fieldnames = keys
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def flatten_result(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in {"evidence_rows", "rejected_domains", "page_logs"}}


def main():
    parser = argparse.ArgumentParser(description="Local Apify-style company/contact/jobs crawler")
    parser.add_argument("--input", required=True, help="Input CSV path")
    parser.add_argument("--output-prefix", required=True, help="Output prefix, e.g. outputs/retail")
    parser.add_argument("--name-col", default="company_name", help="Company name column")
    parser.add_argument("--id-col", default="", help="Optional stable ID column")
    parser.add_argument("--website-col", default="", help="Optional website/domain hint column")
    parser.add_argument("--extra-term-cols", default="", help="Comma-separated columns to add to discovery query")
    parser.add_argument("--mode", choices=["all", "company", "contact", "jobs", "linkedin"], default="all")
    parser.add_argument("--country", default="Australia")
    parser.add_argument("--require-au-domain", action="store_true")
    parser.add_argument("--use-search", action="store_true", help="Use DuckDuckGo fallback. Slower and less reliable.")
    parser.add_argument("--no-clearbit", action="store_true", help="Disable Clearbit autocomplete domain discovery.")
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=12)
    args = parser.parse_args()

    input_path = Path(args.input)
    output_prefix = Path(args.output_prefix)
    rows = read_csv(input_path)
    if args.limit:
        rows = rows[: args.limit]

    extra_cols = [col.strip() for col in args.extra_term_cols.split(",") if col.strip()]
    crawler = LocalCrawler(
        country_hint=args.country,
        require_au_domain=args.require_au_domain,
        max_pages=args.max_pages,
        timeout=args.timeout,
        use_search=args.use_search,
        clearbit=not args.no_clearbit,
    )

    enriched_rows = []
    evidence_rows = []
    rejected_rows = []
    page_rows = []

    def run_one(idx_row):
        idx, row = idx_row
        name = clean_text(row.get(args.name_col))
        row_id = clean_text(row.get(args.id_col)) if args.id_col else row_hash(row, args.name_col)
        website_hint = clean_text(row.get(args.website_col)) if args.website_col else ""
        extra_terms = [clean_text(row.get(col)) for col in extra_cols if clean_text(row.get(col))]
        result = crawler.crawl_company(
            name,
            row_id=row_id,
            website_hint=website_hint,
            extra_terms=extra_terms,
            mode=args.mode,
        )
        flat = row.copy()
        flat.update(flatten_result(result))
        return idx, flat, result["evidence_rows"], result["rejected_domains"], result["page_logs"]

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_one, item): item for item in enumerate(rows, start=1)}
        for done, future in enumerate(as_completed(futures), start=1):
            try:
                idx, flat, evidence, rejected, pages = future.result()
            except Exception as exc:
                idx, row = futures[future]
                name = clean_text(row.get(args.name_col))
                row_id = clean_text(row.get(args.id_col)) if args.id_col else row_hash(row, args.name_col)
                flat = row.copy()
                flat.update(
                    {
                        "row_id": row_id,
                        "company_name": name,
                        "accepted_website": "",
                        "confidence_score": 0,
                        "confidence_label": "Error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                evidence, rejected, pages = [], [], []
            enriched_rows.append((idx, flat))
            evidence_rows.extend(evidence)
            rejected_rows.extend(rejected)
            page_rows.extend(pages)
            if done % 10 == 0 or done == len(rows):
                accepted = sum(1 for _, r in enriched_rows if r.get("accepted_website"))
                print(f"processed {done}/{len(rows)}; accepted websites {accepted}")

    enriched_rows = [row for _, row in sorted(enriched_rows, key=lambda x: x[0])]
    write_csv(output_prefix.with_name(output_prefix.name + "_enriched.csv"), enriched_rows)
    write_csv(output_prefix.with_name(output_prefix.name + "_evidence.csv"), evidence_rows)
    write_csv(output_prefix.with_name(output_prefix.name + "_rejected_domains.csv"), rejected_rows)
    write_csv(output_prefix.with_name(output_prefix.name + "_pages.csv"), page_rows)
    print(f"Saved {len(enriched_rows)} enriched rows to {output_prefix.with_name(output_prefix.name + '_enriched.csv').resolve()}")


if __name__ == "__main__":
    main()
