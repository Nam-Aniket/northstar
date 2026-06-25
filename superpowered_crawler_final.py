#!/usr/bin/env python3
"""
Superpowered Crawler (Final).

Builds on superpowered_crawler.py and adds the four upgrades discussed:

1. Better disguise (TLS/HTTP2 fingerprint) via curl_cffi when installed.
   Falls back transparently to urllib if curl_cffi isn't available.
   Install (free):  pip install curl_cffi

2. Per-host rate limiting (token bucket). No more than --rps requests per
   second per root domain, regardless of thread count. Stops you from
   ever hammering a single site.

3. robots.txt respect. Each host's robots.txt is fetched once and cached;
   disallowed URLs return a FetchResult with status=999 and a reason.
   Use --ignore-robots to disable (not recommended).

4. On-disk page cache. Pages are cached by URL hash in --cache-dir. Reruns
   skip the network entirely for cached URLs. Use --no-cache to disable,
   or --cache-ttl to expire entries.

Plus: --identify mode that sends an honest, contactable User-Agent. Sites
that don't actively defend tend to block honest bots LESS than stealth ones,
and it keeps you on the right side of the line for goodwill scraping.

Everything else (LinkedIn x-ray, summarization, email guessing+SMTP ping,
leadership audit, custom regex rules) is preserved unchanged from
superpowered_crawler.py.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import http.cookiejar
import io
import json
import os
import random
import re
import smtplib
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from local_apify_crawler import (
    LocalCrawler,
    FetchResult,
    PageEvidence,
    clean_text,
    DomainCandidate,
    is_noise_domain,
    domain_of,
    root_domain,
    same_site,
    normalize_url,
    significant_tokens,
)
from dotenv_loader import load_env
load_env()


# Helper functions for company sector and registry lookups
def normalize_company_name_for_matching(name: str) -> str:
    if not name:
        return ""
    # Lowercase
    name = name.lower()
    # Remove common suffixes
    name = re.sub(
        r"\b(pty\s+ltd|pty\s+limited|ltd|limited|inc|incorporated|co|company|group|corp|corporation|plc|services)\b",
        "",
        name,
    )
    # Remove punctuation & whitespace
    name = re.sub(r"[^a-z0-9]", "", name)
    return name.strip()


def map_uk_sic_to_sector(sic: str) -> str:
    if not sic:
        return ""
    # Tech / SaaS
    if sic.startswith(("62", "582", "631")):
        return "Technology / SaaS"
    # Financial Services
    if sic.startswith(("64", "65", "66")):
        return "Financial Services"
    # Healthcare
    if sic.startswith(("86", "212", "325")):
        return "Healthcare"
    # Retail
    if sic.startswith(("47", "45")):
        return "Retail / E-commerce"
    # Energy
    if sic.startswith(("35", "06", "19", "36")):
        return "Energy / Utilities"
    # Consulting / Professional Services
    if sic.startswith(("70", "73", "74", "69", "78")):
        return "Consulting / Professional Services"
    return ""


# ---------------------------------------------------------------------------
# Optional better-disguise transport (curl_cffi).
# ---------------------------------------------------------------------------
# curl_cffi impersonates a real Chrome/Safari TLS + HTTP/2 fingerprint, which
# defeats most JA3/JA4-based anti-bot systems. If it isn't installed we fall
# back to urllib (current behavior). The fallback is silent except for a
# one-time hint on stderr.

try:
    from curl_cffi import requests as cffi_requests  # type: ignore
    CURL_CFFI_AVAILABLE = True
except Exception:  # noqa: BLE001 - we genuinely don't care why it failed
    cffi_requests = None
    CURL_CFFI_AVAILABLE = False

# ---------------------------------------------------------------------------
# Optional PDF parsing transport (pypdf).
# ---------------------------------------------------------------------------
try:
    import pypdf
    PYPDF_AVAILABLE = True
except Exception:
    PYPDF_AVAILABLE = False

# ---------------------------------------------------------------------------
# Optional headless browser fallback (Playwright) for JS-rendered pages.
# Install (free):  pip install playwright && playwright install chromium
# Only invoked when the fast path returns suspiciously empty/blocked content.
# ---------------------------------------------------------------------------
try:
    from playwright.sync_api import sync_playwright  # type: ignore
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False


# Browser profiles curl_cffi knows how to impersonate. We rotate among recent
# real ones. (curl_cffi accepts these as the `impersonate=` argument.)
CURL_IMPERSONATIONS = [
    "chrome120",
    "chrome119",
    "safari17_0",
    "edge101",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]


# ---------------------------------------------------------------------------
# Per-host rate limiter (token bucket, thread-safe).
# ---------------------------------------------------------------------------

class HostRateLimiter:
    """One token bucket per root domain. acquire() blocks until you may fetch."""

    def __init__(self, rps: float = 1.0, burst: int = 1):
        self.rps = max(rps, 0.01)
        self.burst = max(burst, 1)
        self._state: dict[str, dict] = {}  # host -> {tokens, last}
        self._lock = threading.Lock()

    def acquire(self, host: str) -> None:
        if not host:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                st = self._state.setdefault(host, {"tokens": float(self.burst), "last": now})
                elapsed = now - st["last"]
                st["tokens"] = min(self.burst, st["tokens"] + elapsed * self.rps)
                st["last"] = now
                if st["tokens"] >= 1.0:
                    st["tokens"] -= 1.0
                    return
                # how long until we'll have a token
                need = (1.0 - st["tokens"]) / self.rps
            # sleep outside the lock so other hosts aren't blocked
            time.sleep(min(need, 1.0))


# ---------------------------------------------------------------------------
# robots.txt cache.
# ---------------------------------------------------------------------------

class RobotsCache:
    """Fetch and cache robots.txt per host. allowed(url) returns True/False."""

    def __init__(self, user_agent: str = "*"):
        self.user_agent = user_agent
        self._parsers: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> tuple[bool, str]:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        if not host:
            return True, ""
        with self._lock:
            rp = self._parsers.get(host)
            if rp is None:
                rp = urllib.robotparser.RobotFileParser()
                robots_url = f"{parsed.scheme}://{host}/robots.txt"
                rp.set_url(robots_url)
                try:
                    rp.read()
                except Exception:  # noqa: BLE001 - missing/broken robots = allow
                    rp = None
                self._parsers[host] = rp  # cache the negative too
            if rp is None:
                return True, "robots: unreadable, allowing"
            ok = rp.can_fetch(self.user_agent, url)
            return ok, "robots: ok" if ok else "robots: disallowed"


# ---------------------------------------------------------------------------
# Disk cache.
# ---------------------------------------------------------------------------

class DiskCache:
    """Simple URL -> JSON cache on disk. Stores enough to rebuild FetchResult."""

    def __init__(self, cache_dir: str = ".crawler_cache", ttl_seconds: int = 7 * 24 * 3600):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds

    def _path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        # shard into 2-char subdirs so we don't get a single huge dir
        sub = self.dir / key[:2]
        sub.mkdir(parents=True, exist_ok=True)
        return sub / f"{key}.json"

    def get(self, url: str) -> dict | None:
        p = self._path(url)
        if not p.exists():
            return None
        try:
            payload = json.loads(p.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        if self.ttl and time.time() - payload.get("fetched_at", 0) > self.ttl:
            return None
        return payload

    def put(self, url: str, payload: dict) -> None:
        try:
            payload = dict(payload)
            payload["fetched_at"] = time.time()
            self._path(url).write_text(json.dumps(payload), "utf-8")
        except Exception:  # noqa: BLE001 - cache failures must never break a crawl
            pass


# ---------------------------------------------------------------------------
# The crawler.
# ---------------------------------------------------------------------------

class SuperpoweredCrawlerFinal(LocalCrawler):
    """Deep enrichment crawler with disguise + politeness + robots + cache."""

    def __init__(
        self,
        config: dict | None = None,
        rps: float = 1.0,
        cache_dir: str = ".crawler_cache",
        cache_enabled: bool = True,
        cache_ttl: int = 7 * 24 * 3600,
        respect_robots: bool = True,
        identify: bool = False,
        contact_url: str = "",
        proxies: list | None = None,
        polite_delay: float = 1.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.config = config or {}
        self.polite_delay = polite_delay
        self.proxies = proxies or []
        self.identify = identify
        self.contact_url = contact_url or "https://example.invalid/bot"

        # Persistent cookie jar (used by the urllib fallback path).
        self.cookie_jar = http.cookiejar.CookieJar()

        # TLS context for the urllib fallback path (curl_cffi handles its own).
        # Use Python defaults — the custom cipher list was causing handshake
        # timeouts against some hosts (notably html.duckduckgo.com).
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1

        # New: politeness + robots + cache.
        self.rate_limiter = HostRateLimiter(rps=rps, burst=1)
        self.respect_robots = respect_robots
        ua_for_robots = self._identify_ua() if identify else "*"
        self.robots = RobotsCache(user_agent=ua_for_robots)
        self.cache_enabled = cache_enabled
        self.disk_cache = DiskCache(cache_dir=cache_dir, ttl_seconds=cache_ttl) if cache_enabled else None

        if not CURL_CFFI_AVAILABLE:
            print(
                "[hint] curl_cffi not installed - falling back to urllib transport. "
                "Install with `pip install curl_cffi` for a much better browser disguise."
            )

    # ----- helpers ----------------------------------------------------------

    def _identify_ua(self) -> str:
        return f"SuperpoweredCrawler/1.0 (+{self.contact_url})"

    def _host_of(self, url: str) -> str:
        return urllib.parse.urlparse(url).netloc.lower()

    def _pick_ua(self) -> str:
        return self._identify_ua() if self.identify else random.choice(USER_AGENTS)

    def _pick_ua_for(self, url: str) -> str:
        """
        Search engines (DuckDuckGo / Google / Bing) treat identify-mode UAs as
        bot signals and return empty / blocked pages. Always use a rotating
        browser UA for them, even when --identify is on.
        """
        host = self._host_of(url)
        if any(host.endswith(s) for s in (
            "duckduckgo.com", "google.com", "bing.com", "search.brave.com"
        )):
            return random.choice(USER_AGENTS)
        return self._pick_ua()

    def _looks_blocked_or_empty(self, result: FetchResult) -> bool:
        """Heuristic: did the fast path get stonewalled by an anti-bot or JS shell?"""
        if not result.text:
            return True
        if result.status and result.status >= 400:
            return True
        # Tiny body with no real content usually means JS-rendered shell or block page.
        if len(result.text) < 1200:
            return True
        markers = (
            "just a moment", "checking your browser", "cf-chl",
            "captcha", "access denied", "enable javascript",
            "please turn on javascript", "<noscript>",
        )
        low = result.text.lower()
        return any(m in low for m in markers)

    def _fetch_with_playwright(self, url: str, referer: str | None, result: FetchResult) -> None:
        """Last-resort: render the page in headless Chromium and grab the DOM HTML."""
        if not PLAYWRIGHT_AVAILABLE:
            return
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=self._pick_ua(),
                    locale="en-US",
                    viewport={"width": 1366, "height": 850},
                )
                page = context.new_page()
                if referer:
                    page.set_extra_http_headers({"Referer": referer})
                page.goto(url, timeout=int(self.timeout * 1000), wait_until="domcontentloaded")
                # Give late-loaded content a moment
                page.wait_for_timeout(1500)
                result.final_url = page.url
                result.status = 200
                result.content_type = "text/html"
                result.text = page.content()
                browser.close()
                print(f"[playwright] rendered {url}")
        except Exception as exc:  # noqa: BLE001
            result.error = f"playwright: {type(exc).__name__}: {exc}"

    # ----- structured-data extraction --------------------------------------

    def extract_structured_data(self, html: str) -> dict:
        """
        Pull JSON-LD + OpenGraph + meta tags out of an HTML page.
        Returns a dict with merged organization, people, jobs, and OG fields.
        Much more reliable than regex on the rendered text.
        """
        out = {"jsonld": [], "opengraph": {}, "organization": {}, "people": [], "jobs": []}
        if not html:
            return out

        # 1. JSON-LD blocks
        for block in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, flags=re.IGNORECASE | re.DOTALL,
        ):
            try:
                data = json.loads(block.strip())
            except Exception:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                out["jsonld"].append(item)
                t = (item.get("@type") or "").lower() if isinstance(item.get("@type"), str) else ""
                if t in ("organization", "corporation", "ngo", "localbusiness", "nonprofit"):
                    out["organization"] = {
                        "name": item.get("name"),
                        "url": item.get("url"),
                        "logo": item.get("logo") if isinstance(item.get("logo"), str) else None,
                        "email": item.get("email"),
                        "telephone": item.get("telephone"),
                        "same_as": item.get("sameAs"),
                        "address": item.get("address"),
                    }
                elif t == "person":
                    out["people"].append({
                        "name": item.get("name"),
                        "role": item.get("jobTitle"),
                        "email": item.get("email"),
                        "linkedin_url": next(
                            (s for s in (item.get("sameAs") or []) if isinstance(s, str) and "linkedin.com" in s),
                            None,
                        ),
                        "source": "jsonld",
                    })
                elif t == "jobposting":
                    out["jobs"].append({
                        "title": item.get("title"),
                        "description": (item.get("description") or "")[:300],
                        "date_posted": item.get("datePosted"),
                        "employment_type": item.get("employmentType"),
                        "url": item.get("url"),
                    })

        # 2. OpenGraph + meta
        for prop, val in re.findall(
            r'<meta[^>]+property=["\']og:([^"\']+)["\'][^>]+content=["\']([^"\']+)["\']',
            html, flags=re.IGNORECASE,
        ):
            out["opengraph"][prop] = val

        return out

    # ----- the main fetch ---------------------------------------------------

    def fetch(self, url: str, max_bytes: int = 1_500_000, referer: str | None = None, extra_headers: dict | None = None) -> FetchResult:
        """
        Fetch a URL with:
          - disk cache check
          - robots.txt check (bypassed for search APIs)
          - per-host rate limit
          - curl_cffi (browser-impersonating) transport, urllib fallback
          - exponential backoff on 429/403
        """
        started = time.time()
        result = FetchResult(url=url)

        # Recency-windowed endpoints (e.g. LinkedIn job search) MUST be fetched live
        # every run — caching them replays stale results and hides genuinely new jobs.
        # This guard is permanent: it holds even if a caller leaves cache_enabled=True.
        is_volatile = ("seeMoreJobPostings" in url) or ("/jobs-guest/jobs/api/" in url)

        # 1. Cache hit?
        if self.cache_enabled and self.disk_cache is not None and not is_volatile:
            cached = self.disk_cache.get(url)
            if cached is not None:
                result.final_url = cached.get("final_url", url)
                result.status = cached.get("status", 0)
                result.content_type = cached.get("content_type", "")
                result.text = cached.get("text", "")
                result.error = cached.get("error", "")
                result.elapsed_ms = 0
                return result

        # 2. robots.txt (bypassed for official search APIs)
        is_api = "googleapis.com" in url or "api.search.brave.com" in url
        if self.respect_robots and not is_api:
            ok, reason = self.robots.allowed(url)
            if not ok:
                result.status = 999
                result.error = reason
                result.elapsed_ms = int((time.time() - started) * 1000)
                return result

        # 3. rate-limit per host
        self.rate_limiter.acquire(self._host_of(url))

        # 4. fetch with retries
        retries = 3
        backoff = 8.0  # gentler than before; rate limit + cache means we re-hit less
        for attempt in range(retries):
            try:
                if CURL_CFFI_AVAILABLE:
                    self._fetch_with_curl_cffi(url, max_bytes, referer, result, extra_headers)
                else:
                    self._fetch_with_urllib(url, max_bytes, referer, result, extra_headers)

                if result.status and result.status >= 400:
                    if result.status in (429, 403, 503):
                        print(f"[{result.status}] {url} - backing off {backoff:.0f}s (try {attempt+1}/{retries})")
                        time.sleep(backoff)
                        backoff *= 2
                        continue
                break
            except Exception as exc:  # noqa: BLE001
                result.error = f"{type(exc).__name__}: {exc}"
                break

        # 4b. Playwright fallback if the fast path looks blocked/empty (bypassed for search APIs)
        if PLAYWRIGHT_AVAILABLE and not is_api and self._looks_blocked_or_empty(result):
            print(f"[fallback] {url} - fast path empty/blocked, trying Playwright")
            self._fetch_with_playwright(url, referer, result)

        result.elapsed_ms = int((time.time() - started) * 1000)

        # 5. cache successful responses
        if self.cache_enabled and self.disk_cache is not None and result.text and not result.error and not is_volatile:
            self.disk_cache.put(url, {
                "final_url": result.final_url or url,
                "status": result.status,
                "content_type": result.content_type,
                "text": result.text,
                "error": result.error,
            })

        # 6. small jitter so we don't look mechanical even within the rate-limit budget
        if self.polite_delay:
            time.sleep(self.polite_delay * random.uniform(0.5, 1.5))

        return result

    # ----- transport implementations ---------------------------------------

    def _fetch_with_curl_cffi(self, url: str, max_bytes: int, referer: str | None, result: FetchResult, extra_headers: dict | None = None) -> None:
        headers = {
            "User-Agent": self._pick_ua_for(url),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        }
        if referer:
            headers["Referer"] = referer
        if extra_headers:
            headers.update(extra_headers)

        proxies_arg = None
        if self.proxies:
            p = random.choice(self.proxies)
            proxies_arg = {"http": p, "https": p}

        resp = cffi_requests.get(
            url,
            headers=headers,
            impersonate=random.choice(CURL_IMPERSONATIONS),
            timeout=self.timeout,
            proxies=proxies_arg,
            allow_redirects=True,
        )
        data = resp.content[:max_bytes] if resp.content else b""
        # curl_cffi exposes .encoding; fall back to utf-8
        charset = resp.encoding or "utf-8"
        result.final_url = str(resp.url)
        result.status = resp.status_code
        result.content_type = resp.headers.get("content-type", "")
        result.text = self._decode_bytes(data, result.content_type, charset, result.final_url)

    def _fetch_with_urllib(self, url: str, max_bytes: int, referer: str | None, result: FetchResult, extra_headers: dict | None = None) -> None:
        handlers = [urllib.request.HTTPCookieProcessor(self.cookie_jar)]
        if self.proxies:
            p = random.choice(self.proxies)
            handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
        handlers.append(urllib.request.HTTPSHandler(context=self.ssl_context))
        opener = urllib.request.build_opener(*handlers)

        headers = {
            "User-Agent": self._pick_ua_for(url),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin" if referer else "none",
            "Upgrade-Insecure-Requests": "1",
        }
        if referer:
            headers["Referer"] = referer
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                data = resp.read(max_bytes)
                charset = resp.headers.get_content_charset() or "utf-8"
                result.final_url = resp.geturl()
                result.status = resp.status
                result.content_type = resp.headers.get("content-type", "")
                result.text = self._decode_bytes(data, result.content_type, charset, result.final_url)
        except urllib.error.HTTPError as exc:
            result.status = exc.code
            result.error = f"HTTPError: {exc.code}"
            result.final_url = getattr(exc, "url", url)

    def _decode_bytes(self, data: bytes, content_type: str, charset: str, url: str) -> str:
        """Decodes raw bytes into text, handling PDFs natively and deobfuscating Base64 JS emails."""
        text = ""
        # 1. PDF Parsing
        is_pdf = "pdf" in content_type.lower() or url.lower().endswith(".pdf")
        if is_pdf and PYPDF_AVAILABLE:
            try:
                reader = pypdf.PdfReader(io.BytesIO(data))
                pages_text = []
                for page in reader.pages:
                    pages_text.append(page.extract_text() or "")
                text = "\n".join(pages_text)
                print(f"[PDF Extracted] {url} ({len(pages_text)} pages)")
            except Exception as e:
                print(f"[PDF Error] {url}: {e}")
                try:
                    text = data.decode(charset, "ignore")
                except Exception:
                    text = data.decode("utf-8", "ignore")
        else:
            try:
                text = data.decode(charset, "ignore")
            except Exception:
                text = data.decode("utf-8", "ignore")
                
        # 2. Base64 JS Deobfuscation (Hunting for hidden emails)
        if text:
            # Look for Base64 encoded strings in the text
            b64_matches = set(re.findall(r"['\"]([A-Za-z0-9+/]{15,}={0,2})['\"]", text))
            hidden_emails = []
            email_pattern = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
            for b64_str in b64_matches:
                try:
                    decoded_bytes = base64.b64decode(b64_str)
                    decoded_str = decoded_bytes.decode('utf-8').strip()
                    if email_pattern.match(decoded_str):
                        hidden_emails.append(decoded_str)
                except Exception:
                    pass
            
            if hidden_emails:
                print(f"[Base64 Deobfuscation] Found hidden emails on {url}: {hidden_emails}")
                text += "\n" + "\n".join(hidden_emails)
                
        return text

    def search_web(self, query: str, max_results: int = 10) -> list[dict]:
        """
        Unified search helper. Queries Google CSE, Brave Search API, or falls back to scraping.
        Returns a list of dicts: [{"title": str, "link": str, "snippet": str}]
        """
        google_key = os.environ.get("GOOGLE_CSE_KEY")
        google_cx = os.environ.get("GOOGLE_CSE_CX")
        if google_key and google_cx:
            print(f"[search] Using Google CSE API for: {query}")
            try:
                return self._search_google_cse(query, google_key, google_cx, max_results)
            except Exception as e:
                print(f"[search] Google CSE failed: {e}. Trying Brave API...")
        
        brave_key = os.environ.get("BRAVE_API_KEY")
        if brave_key:
            print(f"[search] Using Brave Search API for: {query}")
            try:
                return self._search_brave_api(query, brave_key, max_results)
            except Exception as e:
                print(f"[search] Brave Search API failed: {e}. Trying fallback...")
        
        print(f"[search] Using HTML scrape fallback for: {query}")
        return self._search_scrape_fallback(query, max_results)

    def _search_google_cse(self, query: str, api_key: str, cx: str, max_results: int = 10) -> list[dict]:
        params = {
            "key": api_key,
            "cx": cx,
            "q": query,
            "num": min(max_results, 10)
        }
        url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode(params)
        fetch = self.fetch(url, max_bytes=500_000)
        if fetch.error or not fetch.text:
            raise Exception(f"Google CSE fetch error: {fetch.error}")
        
        data = json.loads(fetch.text)
        results = []
        for item in data.get("items", []):
            results.append({
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })
        return results

    def _search_brave_api(self, query: str, api_key: str, max_results: int = 10) -> list[dict]:
        params = {
            "q": query,
            "count": min(max_results, 20)
        }
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(params)
        headers = {
            "X-Subscription-Token": api_key,
            "Accept": "application/json"
        }
        fetch = self.fetch(url, max_bytes=500_000, extra_headers=headers)
        if fetch.error or not fetch.text:
            raise Exception(f"Brave API fetch error: {fetch.error}")
        
        data = json.loads(fetch.text)
        results = []
        web_results = data.get("web", {}).get("results", [])
        for item in web_results:
            results.append({
                "title": item.get("title", ""),
                "link": item.get("url", ""),
                "snippet": item.get("description", ""),
            })
        return results[:max_results]

    def _search_scrape_fallback(self, query: str, max_results: int = 10) -> list[dict]:
        text = ""
        for base in ("https://search.brave.com/search?", "https://www.bing.com/search?"):
            url = base + urllib.parse.urlencode({"q": query})
            fetch = self.fetch(url, max_bytes=800_000)
            if fetch.text and not fetch.error:
                text = fetch.text
                break
        if not text:
            return []
        
        results = []
        seen_links = set()
        
        anchor_re = re.compile(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        for m in anchor_re.finditer(text):
            href = m.group(1).replace("&amp;", "&")
            host = urllib.parse.urlparse(href).netloc.lower()
            if not host or host in seen_links:
                continue
            if any(h in host for h in (
                "brave.com", "bing.com", "microsoft.com",
                "duckduckgo.com", "google.com",
            )):
                continue
            seen_links.add(host)
            
            anchor_text = clean_text(re.sub(r"<[^>]+>", " ", m.group(2) or ""))
            
            tail = text[m.end(): m.end() + 1500]
            snip_m = re.search(
                r'(?:snippet-description|snippet-content|b_lineclamp|b_caption|description)[^>]*>(.*?)<',
                tail, re.IGNORECASE | re.DOTALL,
            )
            snippet = ""
            if snip_m:
                snippet = clean_text(re.sub(r"<[^>]+>", " ", snip_m.group(1) or ""))
            
            results.append({
                "title": anchor_text or host,
                "link": href,
                "snippet": snippet
            })
            if len(results) >= max_results:
                break
        return results

    # =======================================================================
    # Everything below this line is preserved from superpowered_crawler.py
    # (LinkedIn x-ray, summarization, SMTP ping, email guessing, leadership
    # audit, custom regex extraction, deep crawl orchestrator). Unchanged
    # behavior, just inherits the upgraded fetch() above.
    # =======================================================================

    # ----- Registry Resolvers & Custom Sector Verification Overrides -----

    def load_wgea_cache(self) -> dict[str, dict]:
        """Load and cache the local WGEA dataset if it exists."""
        if hasattr(self, "_wgea_cache_data"):
            return self._wgea_cache_data
            
        self._wgea_cache_data = {}
        paths_to_check = [
            Path("wgea_companies.csv"),
            Path("wgea.csv"),
        ]
        
        csv_path = None
        for p in paths_to_check:
            if p.exists():
                csv_path = p
                break
                
        if not csv_path:
            return self._wgea_cache_data
            
        print(f"  [wgea] Loading local WGEA registry from {csv_path}...")
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    org_name = ""
                    for k in ["organisation_name", "organisation name", "company_name", "company name", "name", "Organisation name", "Company Name"]:
                        if k in row and row[k]:
                            org_name = row[k]
                            break
                    if not org_name:
                        first_col = list(row.keys())[0] if row.keys() else ""
                        if first_col:
                            org_name = row[first_col]
                    
                    if not org_name:
                        continue
                        
                    key = normalize_company_name_for_matching(org_name)
                    if not key:
                        continue
                        
                    sector = ""
                    for k in ["industry", "sector", "anzsic_division", "anzsic division", "anzsic_industry_division", "Industry division", "ANZSIC Industry Division"]:
                        if k in row and row[k]:
                            sector = row[k]
                            break
                            
                    employees = None
                    for k in ["employees", "employee_count", "size", "employee count", "Number of employees", "Total Employees"]:
                        if k in row and row[k]:
                            try:
                                employees = int(row[k])
                                break
                            except ValueError:
                                pass
                                
                    website = ""
                    for k in ["website", "domain", "website_url", "url", "Website"]:
                        if k in row and row[k]:
                            website = row[k]
                            break
                            
                    self._wgea_cache_data[key] = {
                        "name": org_name,
                        "sector": sector,
                        "employees": employees,
                        "url": website,
                        "country": "Australia",
                        "source": "wgea"
                    }
            print(f"  [wgea] Loaded {len(self._wgea_cache_data)} companies from WGEA registry.")
        except Exception as e:
            print(f"  [wgea] Error loading registry: {e}")
            
        return self._wgea_cache_data

    def resolve_via_local_wgea_cache(self, company_name: str) -> dict | None:
        cache = self.load_wgea_cache()
        if not cache:
            return None
        key = normalize_company_name_for_matching(company_name)
        return cache.get(key)

    def resolve_via_companies_house(self, company_name: str) -> dict | None:
        """Query UK Companies House API for a company."""
        api_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
        if not api_key:
            return None
        
        clean_name = company_name.strip()
        if not clean_name:
            return None
            
        auth_str = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("utf-8")
        headers = {"Authorization": f"Basic {auth_str}"}
        
        url = f"https://api.company-information.service.gov.uk/search/companies?q={urllib.parse.quote(clean_name)}"
        fetch = self.fetch(url, extra_headers=headers)
        if fetch.error or not fetch.text:
            return None
            
        try:
            data = json.loads(fetch.text)
            items = data.get("items", [])
            if not items:
                return None
            
            best_match = None
            for item in items[:5]:
                status = item.get("company_status", "")
                if status == "active":
                    best_match = item
                    break
            
            if not best_match:
                best_match = items[0]
                
            company_number = best_match.get("company_number")
            company_title = best_match.get("title", company_name)
            
            profile_url = f"https://api.company-information.service.gov.uk/company/{company_number}"
            profile_fetch = self.fetch(profile_url, extra_headers=headers)
            if profile_fetch.error or not profile_fetch.text:
                return {
                    "name": company_title,
                    "country": "United Kingdom",
                    "sector": "",
                    "sic_codes": [],
                    "company_number": company_number,
                    "source": "companies_house"
                }
                
            profile_data = json.loads(profile_fetch.text)
            sic_codes = profile_data.get("sic_codes", [])
            
            mapped_sectors = set()
            for sic in sic_codes:
                sec = map_uk_sic_to_sector(sic)
                if sec:
                    mapped_sectors.add(sec)
            
            sector_str = ", ".join(mapped_sectors) if mapped_sectors else ""
            
            return {
                "name": company_title,
                "country": "United Kingdom",
                "sector": sector_str,
                "sic_codes": sic_codes,
                "company_number": company_number,
                "source": "companies_house"
            }
        except Exception as e:
            print(f"  [companies_house] Error querying: {e}")
            return None

    def get_wikidata_labels(self, qids: list[str]) -> dict[str, str]:
        if not qids:
            return {}
        qids = [q for q in qids if q and q.startswith("Q")]
        if not qids:
            return {}
        qids_str = "|".join(qids)
        url = f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={urllib.parse.quote(qids_str)}&props=labels&languages=en&format=json"
        fetch = self.fetch(url)
        if fetch.error or not fetch.text:
            return {}
        try:
            data = json.loads(fetch.text)
            entities = data.get("entities", {})
            labels = {}
            for qid, ent in entities.items():
                label = ent.get("labels", {}).get("en", {}).get("value", "")
                if label:
                    labels[qid] = label
            return labels
        except Exception as e:
            print(f"  [wikidata] Error parsing labels: {e}")
            return {}

    def resolve_via_wikidata(self, company_name: str) -> dict | None:
        """Query Wikidata for a company name."""
        clean_name = company_name.strip()
        if not clean_name:
            return None
        
        url = f"https://www.wikidata.org/w/api.php?action=wbsearchentities&search={urllib.parse.quote(clean_name)}&language=en&format=json&type=item"
        fetch = self.fetch(url)
        if fetch.error or not fetch.text:
            return None
        try:
            data = json.loads(fetch.text)
            search_results = data.get("search", [])
            if not search_results:
                return None
            
            best_match = search_results[0]
            entity_id = best_match.get("id")
            entity_name = best_match.get("label", company_name)
        except Exception as e:
            print(f"  [wikidata] Error searching: {e}")
            return None
            
        url_claims = f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={entity_id}&props=claims&languages=en&format=json"
        fetch_claims = self.fetch(url_claims)
        if fetch_claims.error or not fetch_claims.text:
            return None
        
        try:
            data_claims = json.loads(fetch_claims.text)
            entity = data_claims.get("entities", {}).get(entity_id, {})
            claims = entity.get("claims", {})
            
            websites = claims.get("P856", [])
            website_url = ""
            if websites:
                website_url = websites[0].get("mainsnak", {}).get("datavalue", {}).get("value", "")
            
            if not website_url:
                return None
                
            countries = claims.get("P17", [])
            country_qid = ""
            if countries:
                val = countries[0].get("mainsnak", {}).get("datavalue", {}).get("value", {})
                if isinstance(val, dict):
                    country_qid = val.get("id", "")
                    
            industries = claims.get("P452", [])
            industry_qids = []
            for ind in industries:
                val = ind.get("mainsnak", {}).get("datavalue", {}).get("value", {})
                if isinstance(val, dict):
                    qid = val.get("id", "")
                    if qid:
                        industry_qids.append(qid)
                        
            employees = None
            for p_code in ["P1128", "P2196"]:
                emp_claims = claims.get(p_code, [])
                if emp_claims:
                    val = emp_claims[0].get("mainsnak", {}).get("datavalue", {}).get("value", {})
                    if isinstance(val, dict) and "amount" in val:
                        try:
                            employees = int(val["amount"].lstrip("+"))
                            break
                        except ValueError:
                            pass
            
            all_qids = []
            if country_qid:
                all_qids.append(country_qid)
            all_qids.extend(industry_qids)
            
            labels = self.get_wikidata_labels(all_qids)
            
            country_name = labels.get(country_qid, "")
            industry_names = [labels.get(qid, "") for qid in industry_qids if qid in labels]
            sector_str = ", ".join(industry_names) if industry_names else ""
            
            return {
                "url": website_url,
                "name": entity_name,
                "country": country_name,
                "sector": sector_str,
                "employees": employees,
                "source": "wikidata"
            }
        except Exception as e:
            print(f"  [wikidata] Error fetching claims: {e}")
            return None

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

        # 1. Query Registry / DB Resolvers first
        resolved_info = None
        country = (self.country_hint or "").lower()

        # Australia WGEA check
        if "australia" in country or "au" == country:
            resolved_info = self.resolve_via_local_wgea_cache(company_name)

        # UK Companies House check
        if not resolved_info and ("united kingdom" in country or "uk" == country or "gb" == country):
            resolved_info = self.resolve_via_companies_house(company_name)

        # Wikidata check (global)
        if not resolved_info:
            resolved_info = self.resolve_via_wikidata(company_name)

        # If we successfully resolved the company name and website via registries
        if resolved_info and resolved_info.get("url"):
            url = resolved_info["url"]
            source = resolved_info.get("source", "registry")
            self._last_resolved_metadata = resolved_info
            
            print(f"  [resolver:{source}] Resolved {company_name} -> {url} (Sector: {resolved_info.get('sector')}, Country: {resolved_info.get('country')})")
            
            candidates.append(
                DomainCandidate(
                    url=normalize_url(url),
                    source=source,
                    candidate_name=resolved_info.get("name", company_name),
                    confidence_hint=95
                )
            )
            return candidates

        # If a registry gave us details but no website, use them to refine name
        if resolved_info:
            refined_company_name = resolved_info.get("name", company_name)
            self._last_resolved_metadata = resolved_info
        else:
            refined_company_name = company_name
            self._last_resolved_metadata = None

        # 2. Fallback to Search Query/Clearbit
        terms = self.candidate_terms(refined_company_name, extra_terms)
        if self.clearbit:
            for term in terms[:4]:
                candidates.extend(self.clearbit_candidates(term, refined_company_name))

        candidates.extend(self.guessed_domain_candidates(refined_company_name))

        if self.use_search:
            tld_map = {
                "australia": "com.au",
                "au": "com.au",
                "united kingdom": "co.uk",
                "uk": "co.uk",
                "gb": "co.uk",
                "saudi arabia": "com.sa",
                "sa": "com.sa",
                "united states": "com",
                "us": "com",
                "canada": "ca",
                "ca": "ca",
            }
            tld = tld_map.get(country, "")
            site_suffix = f" site:.{tld}" if tld else ""

            target_sectors = self.config.get("target_sectors", [])
            sector_keywords = self.config.get("sector_keywords", {})
            sector_keyword = ""
            if target_sectors and sector_keywords:
                all_keywords = []
                for sec in target_sectors:
                    kws = sector_keywords.get(sec, [])
                    if kws:
                        all_keywords.append(kws[0])
                if all_keywords:
                    sector_keyword = f" ({' OR '.join(all_keywords)})"

            for term in terms[:2]:
                query = f'"{term}" ("contact" OR "about" OR "official website")'
                if sector_keyword:
                    query += sector_keyword
                if site_suffix:
                    query += site_suffix
                
                print(f"  [search] Refined fallback query: {query}")
                candidates.extend(self.duckduckgo_candidates(query, refined_company_name))

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

    def check_sector_keywords(self, text: str) -> str | None:
        """Check text against sector keywords to determine matched sector."""
        target_sectors = self.config.get("target_sectors", [])
        sector_keywords = self.config.get("sector_keywords", {})
        if not target_sectors or not sector_keywords:
            return None
            
        text_lower = text.lower()
        best_sector = None
        max_score = 0
        
        for sector in target_sectors:
            keywords = sector_keywords.get(sector, [])
            if not keywords:
                continue
            distinct_hits = 0
            total_freq = 0
            for kw in keywords:
                count = text_lower.count(kw.lower())
                if count > 0:
                    distinct_hits += 1
                    total_freq += count
            
            if distinct_hits >= 2 or total_freq >= 4:
                score = distinct_hits * 10 + total_freq
                if score > max_score:
                    max_score = score
                    best_sector = sector
                    
        return best_sector

    def classify_sector_via_llm(self, company_name: str, page_text: str) -> str:
        """Classify sector using LLM."""
        target_sectors = self.config.get("target_sectors", [])
        if not target_sectors:
            return "Other"
            
        system_prompt = (
            "You are an expert corporate analyst. Your task is to classify the industry/sector of a company "
            "based on the text content from its website homepage.\n"
            f"Allowed sectors: {json.dumps(target_sectors)}\n"
            "If the company does not fit into any of these sectors, respond with 'Other'.\n"
            "Respond ONLY with the exact sector name or 'Other' (no explanation, no punctuation)."
        )
        user_prompt = f"Company Name: {company_name}\nWebsite Content Snippet:\n{page_text[:4000]}"
        
        res = self.call_llm(system_prompt, user_prompt, max_tokens=10)
        res_clean = res.strip().strip("'\"")
        
        for sector in target_sectors:
            if res_clean.lower() == sector.lower() or sector.lower() in res_clean.lower():
                return sector
        return "Other"

    def validate_domain(self, company_name: str, candidate: DomainCandidate, fetch: FetchResult, page: PageEvidence) -> tuple[bool, int, str]:
        # Call base validation
        ok, score, reason = super().validate_domain(company_name, candidate, fetch, page)
        if not ok:
            return False, score, reason
            
        target_sectors = self.config.get("target_sectors", [])
        if not target_sectors:
            return True, score, "accepted"
            
        resolved_sector = None
        cand_domain = urllib.parse.urlparse(candidate.url).netloc.replace("www.", "").lower()
        
        if hasattr(self, "_last_resolved_metadata") and self._last_resolved_metadata:
            meta = self._last_resolved_metadata
            meta_url = meta.get("url", "")
            meta_domain = urllib.parse.urlparse(meta_url).netloc.replace("www.", "").lower() if meta_url else ""
            if cand_domain == meta_domain or (meta.get("name") and meta["name"].lower() == company_name.lower()):
                resolved_sector = meta.get("sector", "")
                
        if resolved_sector:
            matched_sector = None
            resolved_sector_lower = resolved_sector.lower()
            sector_keywords = self.config.get("sector_keywords", {})
            for sector in target_sectors:
                if sector.lower() in resolved_sector_lower:
                    matched_sector = sector
                    break
                kws = sector_keywords.get(sector, [])
                if any(kw.lower() in resolved_sector_lower for kw in kws):
                    matched_sector = sector
                    break
                    
            if matched_sector:
                print(f"  [sector:registry] Company {company_name} matched sector '{matched_sector}' via registry metadata: '{resolved_sector}'")
                return True, score, f"accepted ({matched_sector} via registry)"
                
        evidence_text = " ".join([page.title, page.meta_description, page.text]).lower()
        matched_sector = self.check_sector_keywords(evidence_text)
        if matched_sector:
            print(f"  [sector:keywords] Company {company_name} matched sector '{matched_sector}' via keyword density.")
            return True, score, f"accepted ({matched_sector} via keywords)"
            
        print(f"  [sector:llm] Keyword density ambiguous for {company_name}. Falling back to LLM...")
        llm_sector = self.classify_sector_via_llm(company_name, page.text)
        if llm_sector and llm_sector != "Other":
            print(f"  [sector:llm] LLM classified {company_name} as '{llm_sector}'.")
            return True, score, f"accepted ({llm_sector} via LLM)"
            
        print(f"  [sector:reject] Company {company_name} rejected: does not match any target sectors.")
        return False, 0, "rejected: out of sector"

    def duckduckgo_candidates(self, query: str, company_name: str) -> list:
        """
        Override of LocalCrawler.duckduckgo_candidates that uses search_web.
        Same return shape so the rest of the base crawler keeps working.
        """
        from local_apify_crawler import DomainCandidate, is_noise_domain, domain_of

        search_results = self.search_web(query, max_results=10)
        if not search_results:
            return []

        candidates = []
        seen = set()
        for item in search_results:
            href = item.get("link", "").replace("&amp;", "&")
            if not href.startswith("https://") and not href.startswith("http://"):
                continue
            host = urllib.parse.urlparse(href).netloc.lower()
            if not host or host in seen:
                continue
            if any(h in host for h in (
                "brave.com", "bing.com", "microsoft.com",
                "duckduckgo.com", "google.com",
                "linkedin.com", "facebook.com", "twitter.com",
                "youtube.com", "instagram.com",
            )):
                continue
            if is_noise_domain(href):
                continue
            if self.require_au_domain and not domain_of(href).endswith(".au"):
                continue
            seen.add(host)
            score = self.name_domain_score(company_name, "", domain_of(href))
            candidates.append(DomainCandidate(href, "search_web", confidence_hint=max(score, 12)))
            if len(candidates) >= 8:
                break
        return candidates

    def xray_linkedin(self, company_name: str) -> list[dict]:
        people = []
        roles = self.config.get("target_roles", ["Director", "Manager", "Head", "VP"])
        roles_query = " OR ".join(f'"{role}"' for role in roles)
        
        country_clause = f' "{self.country_hint}"' if self.country_hint else ''
        query = f'site:linkedin.com/in "{company_name}" ({roles_query}){country_clause}'

        print(f"  [xray:{company_name}] Running web search query: {query}")
        search_results = self.search_web(query, max_results=10)
        if not search_results:
            return []

        for item in search_results:
            href = item.get("link", "").replace("&amp;", "&")
            parsed = urllib.parse.urlparse(href)
            qs = urllib.parse.parse_qs(parsed.query)
            if "uddg" in qs:
                href = qs["uddg"][0]
            href = urllib.parse.unquote(href)

            if not href.startswith("https://") or "linkedin.com/in/" not in href:
                continue

            # Parse title to get name & role
            title_clean = item.get("title", "")
            for suffix in ("| LinkedIn", "- LinkedIn", "– LinkedIn", "on LinkedIn"):
                if suffix in title_clean:
                    title_clean = title_clean.split(suffix)[0].strip()

            parts = re.split(r"\s+[-–|]\s+", title_clean)
            name = parts[0].strip() if parts else "Unknown"
            role = parts[1].strip() if len(parts) > 1 else "Unknown"
            snippet = clean_text(item.get("snippet", ""))

            people.append({
                "name": name,
                "role": role,
                "linkedin_url": href,
                "snippet": snippet,
            })
        return people

    def local_extractive_summarization(self, text: str) -> dict:
        if not text:
            return {"summary": "", "value_prop": "", "hook": ""}

        sentences = re.split(r"(?<=[.!?]) +", text)
        sentences = [s.strip() for s in sentences if len(s.split()) > 5]

        vp_keywords = {
            "we provide", "platform", "helps", "solution", "mission",
            "leading", "enables", "designed to", "dedicated to",
        }

        scored_sentences = []
        for s in sentences:
            s_lower = s.lower()
            score = sum(1 for kw in vp_keywords if kw in s_lower)
            if len(scored_sentences) < 3:
                score += 1
            if score > 0:
                scored_sentences.append((score, s))

        scored_sentences.sort(key=lambda x: x[0], reverse=True)
        best_sentences = [s[1] for s in scored_sentences[:2]]
        value_prop = " ".join(best_sentences) if best_sentences else (text[:300] + "...")

        return {
            "summary": text[:200] + "...",
            "value_prop": value_prop,
            "hook": f"I noticed you help with {value_prop[:50].lower()}...",
        }

    def active_smtp_ping(self, email: str, domain: str) -> str:
        try:
            output = subprocess.check_output(
                ["host", "-t", "MX", domain],
                stderr=subprocess.STDOUT,
                timeout=5,
            )
            output_str = output.decode("utf-8")
            # Capture the FULL MX hostname (the old non-greedy `(.*?)\.` grabbed
            # only the first label, e.g. 'aspmx' instead of 'aspmx.l.google.com').
            mx_records = [h.rstrip(".") for h in re.findall(r"mail is handled by \d+ (\S+)", output_str)]
            if not mx_records:
                return "Failed: No MX Records Found"
            mx_record = mx_records[0].strip()

            server = smtplib.SMTP(timeout=5)
            server.connect(mx_record)
            server.helo(socket.getfqdn())
            server.mail("ping@" + domain)
            code, _ = server.rcpt(email)
            server.quit()

            if code == 250:
                return "Valid"
            if code >= 500:
                return "Invalid"
            return "Unknown"
        except Exception as e:  # noqa: BLE001
            return f"Failed: {type(e).__name__}"

    def format_email_guess(self, pattern: str, first: str, last: str, domain: str) -> str:
        try:
            return pattern.format(first=first, last=last, domain=domain)
        except Exception:
            return f"{first}.{last}@{domain}"

    def call_llm(self, system_prompt: str, user_prompt: str, max_tokens: int = 150) -> str:
        """
        Call LLM using configured credentials in environmental variables.
        """
        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("[llm] LLM_API_KEY is not configured.")
            return ""
        
        base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
        model = os.environ.get("LLM_MODEL", "deepseek-chat")
        
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }).encode("utf-8")
        
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "ignore")
            print(f"[llm] HTTP Error {exc.code}: {body}")
            return ""
        except Exception as e:
            print(f"[llm] Request failed: {e}")
            return ""

    def resolve_company_email_pattern(self, company_name: str, domain: str) -> str | None:
        """
        Query search APIs for email patterns and use LLM to extract the python format.
        """
        if not domain:
            return None
        
        query = f'"{domain}" "email format" OR "email pattern"'
        print(f"  [pattern:{company_name}] Searching for email format snippets: {query}")
        search_results = self.search_web(query, max_results=5)
        
        snippets = []
        for item in search_results:
            snippets.append(f"Title: {item.get('title')}\nSnippet: {item.get('snippet')}\n")
        
        if not snippets:
            print(f"  [pattern:{company_name}] No search results found for pattern query.")
            return None
        
        snippets_context = "\n".join(snippets)
        
        system_prompt = (
            "You are an expert data extraction assistant. Your job is to analyze search engine snippets "
            "and identify the dominant email pattern for a company.\n"
            "Respond ONLY with the matched pattern in python string formatting format, or 'Unknown' if not found.\n"
            "Do not include any explanation or extra text."
        )
        
        user_prompt = (
            f"Company Name: {company_name}\n"
            f"Domain: {domain}\n\n"
            f"Search snippets:\n{snippets_context}\n\n"
            "Analyze the snippets and identify the email pattern. "
            "Common patterns include:\n"
            "- {first}.{last}@{domain} (e.g. john.doe@company.com)\n"
            "- {first}@{domain} (e.g. john@company.com)\n"
            "- {first[0]}{last}@{domain} (e.g. jdoe@company.com)\n"
            "- {first}{last[0]}@{domain} (e.g. johnd@company.com)\n"
            "- {first}.{last[0]}@{domain} (e.g. john.d@company.com)\n"
            "- {first[0]}.{last}@{domain} (e.g. j.doe@company.com)\n"
            "- {first}{last}@{domain} (e.g. johndoe@company.com)\n\n"
            "Output ONLY the python format string (e.g., {first}.{last}@{domain}), or 'Unknown'."
        )
        
        response = self.call_llm(system_prompt, user_prompt, max_tokens=30)
        print(f"  [pattern:{company_name}] LLM raw response: {response!r}")
        
        # Clean response and look for brackets
        match = re.search(r"\{[a-zA-Z0-9_\[\]]+\}.*?@\{domain\}", response)
        if match:
            pattern = match.group(0)
            # Ensure it is a valid format string by validating brackets
            try:
                # Test format
                pattern.format(first="john", last="doe", domain=domain)
                print(f"  [pattern:{company_name}] Resolved pattern successfully: {pattern}")
                return pattern
            except Exception:
                pass
        
        print(f"  [pattern:{company_name}] Could not resolve pattern.")
        return None

    def guess_and_verify_emails_locally(self, people: list[dict], domain: str, company_name: str | None = None) -> list[dict]:
        if not domain:
            return people

        is_catch_all = False
        fake_email = f"fake-test-123456789xyz@{domain}"
        ping_res = self.active_smtp_ping(fake_email, domain)
        if ping_res == "Valid":
            is_catch_all = True
            print(f"[{domain}] Detected Catch-All server. Falling back to web search validation.")

        # Load domain cache to resolve or update patterns
        cache = {}
        cache_path = Path("company_domains.json")
        if cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text("utf-8"))
            except Exception as e:
                print(f"Error reading company_domains.json: {e}")
        
        pattern = None
        # 1. Look up by company name
        company_entry = None
        if company_name:
            if company_name in cache:
                company_entry = cache[company_name]
            else:
                for name, entry in cache.items():
                    if name.lower() == company_name.lower():
                        company_entry = entry
                        company_name = name
                        break
        
        # 2. Look up by domain
        if not company_entry and domain:
            for name, entry in cache.items():
                if entry.get("domain", "").lower() == domain.lower():
                    company_entry = entry
                    if not company_name:
                        company_name = name
                    break
        
        if company_entry:
            pattern = company_entry.get("pattern")
            if not company_entry.get("domain") and domain:
                company_entry["domain"] = domain
                try:
                    cache_path.write_text(json.dumps(cache, indent=2), "utf-8")
                except Exception:
                    pass
        
        # 3. If no pattern resolved, use the multi-source resolver
        # (replaces the old search-engine-dependent resolve_company_email_pattern which is broken
        #  on this ISP because search_web() → DDG/Brave/Bing are all blocked)
        if not pattern and company_name and domain:
            try:
                from email_pattern_resolver import resolve_pattern_for_company
                resolved, src = resolve_pattern_for_company(self, company_name, domain)
            except ImportError:
                resolved, src = self.resolve_company_email_pattern(company_name, domain), "llm-search"
            if resolved:
                pattern = resolved
                if company_name not in cache:
                    cache[company_name] = {}
                cache[company_name]["domain"] = domain
                cache[company_name]["pattern"] = resolved
                cache[company_name]["pattern_source"] = src
                cache[company_name]["_note"] = f"confirmed via {src}"
                try:
                    cache_path.write_text(json.dumps(cache, indent=2), "utf-8")
                    print(f"Saved resolved pattern to company_domains.json for {company_name}")
                except Exception as e:
                    print(f"Error saving to company_domains.json: {e}")

        verified = []
        for person in people:
            name_parts = person["name"].lower().split()
            if len(name_parts) >= 2:
                first = re.sub(r"[^a-z]", "", name_parts[0])
                last = re.sub(r"[^a-z]", "", name_parts[-1])
                
                guesses = []
                if pattern:
                    guessed = self.format_email_guess(pattern, first, last, domain)
                    guesses.append(guessed)
                
                # Add fallbacks
                for g in [f"{first}.{last}@{domain}", f"{first}@{domain}", f"{first[0]}{last}@{domain}"]:
                    if g not in guesses:
                        guesses.append(g)

                person["guessed_emails"] = guesses
                person["verified_email"] = None
                person["verification_source"] = None

                # Try SMTP ping first if reachable and not catch-all
                if not is_catch_all and not ping_res.startswith("Failed"):
                    for guess in guesses:
                        validity = self.active_smtp_ping(guess, domain)
                        if validity == "Valid":
                            person["verified_email"] = guess
                            person["verification_source"] = "Active SMTP Ping"
                            break
                    if not person["verified_email"]:
                        person["verified_email"] = "Invalid (all SMTP guesses failed)"
                else:
                    # Catch-all or unreachable/failed: search the open web for the email using search_web
                    for guess in guesses:
                        query = f'"{guess}"'
                        print(f"  [verify:{domain}] Searching web for email: {query}")
                        search_results = self.search_web(query, max_results=5)
                        found = False
                        for item in search_results:
                            text_to_check = (item.get("title", "") + " " + item.get("snippet", "") + " " + item.get("link", "")).lower()
                            if guess.lower() in text_to_check:
                                person["verified_email"] = guess
                                person["verification_source"] = "Web search (unified)"
                                found = True
                                break
                        if found:
                            break
                    if not person["verified_email"]:
                        person["verified_email"] = f"Unverified Local Guess: {guesses[0]}"

                verified.append(person)
            else:
                verified.append(person)
        return verified

    def custom_extraction(self, text: str) -> dict:
        custom = {}
        rules = self.config.get("custom_regex_rules", {})
        for key, pattern in rules.items():
            matches = re.findall(pattern, text, flags=re.IGNORECASE)
            if matches:
                custom[key] = matches[0] if isinstance(matches[0], str) else str(matches[0])
        return custom

    def verify_leadership_page(self, url: str, referer: str | None = None) -> dict:
        audit = {
            "scraped_board_total": 0,
            "scraped_board_women": 0,
            "scraped_board_men": 0,
            "scraped_board_unknown": 0,
            "scraped_exec_total": 0,
            "scraped_exec_women": 0,
            "scraped_exec_men": 0,
            "scraped_exec_unknown": 0,
            "source_url": url,
            "bios_found": [],
        }
        if not url:
            return audit

        fetch = self.fetch(url, referer=referer)
        if fetch.error or not fetch.text:
            return audit

        paragraphs = re.split(r"<(?:p|div|li)[^>]*>", fetch.text, flags=re.IGNORECASE)

        seen_bios = set()
        for p in paragraphs:
            clean_p = clean_text(re.sub(r"<[^>]+>", " ", p))
            if not clean_p or clean_p in seen_bios:
                continue

            word_count = len(clean_p.split())
            if word_count < 15 or word_count > 200:
                continue

            low_p = " " + clean_p.lower() + " "
            she_count = len(re.findall(r"\b(?:she|her|hers)\b", low_p))
            he_count = len(re.findall(r"\b(?:he|him|his)\b", low_p))

            if she_count > 0 or he_count > 0:
                seen_bios.add(clean_p)
                is_board = bool(re.search(r"\b(?:board|director|chair|chairperson|trustee|non-executive)\b", low_p))
                is_exec = bool(re.search(r"\b(?:executive|chief|ceo|cfo|coo|manager|head|president|lead)\b", low_p))
                cat = "board" if (is_board and not is_exec) or (is_board and "executive director" not in low_p) else "exec"

                audit[f"scraped_{cat}_total"] += 1
                if she_count > he_count:
                    audit[f"scraped_{cat}_women"] += 1
                    gender = "Woman"
                elif he_count > she_count:
                    audit[f"scraped_{cat}_men"] += 1
                    gender = "Man"
                else:
                    audit[f"scraped_{cat}_unknown"] += 1
                    gender = "Unknown"

                audit["bios_found"].append({
                    "snippet": clean_p[:100] + "...",
                    "inferred_gender": gender,
                    "category": cat,
                })
        return audit

    def crawl_company_deep(self, company_name: str, **kwargs) -> dict:
        base_result = {}
        people = []

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_base = executor.submit(self.crawl_company, company_name, **kwargs)
            future_xray = executor.submit(self.xray_linkedin, company_name)
            base_result = future_base.result()
            people = future_xray.result()

        if not base_result.get("accepted_website"):
            base_result["people"] = []
            base_result["personalization"] = {}
            return base_result

        domain = urllib.parse.urlparse(base_result["accepted_website"]).netloc.replace("www.", "")

        print(f"Locally verifying emails for extracted people at {domain}...")
        people = self.guess_and_verify_emails_locally(people, domain, company_name=company_name)
        base_result["people"] = people

        fetch = self.fetch(base_result["accepted_website"])
        page = self.parse_page(fetch)
        about_text = page.meta_description + " " + page.text[:5000]

        # Structured-data extraction (JSON-LD + OpenGraph) runs BEFORE regex.
        # Anything we find here is higher-confidence than regex hits.
        structured = self.extract_structured_data(fetch.text or "")
        base_result["structured_data"] = structured

        # Merge any JSON-LD-discovered people into the people list (deduped by name).
        if structured.get("people"):
            existing_names = {p.get("name", "").lower() for p in people}
            for sp in structured["people"]:
                if sp.get("name") and sp["name"].lower() not in existing_names:
                    people.append(sp)
                    existing_names.add(sp["name"].lower())
            base_result["people"] = people

        print(f"Running local NLP summarization for {company_name}...")
        personalization = self.local_extractive_summarization(about_text)

        if self.config.get("custom_regex_rules"):
            print(f"Running custom extractions for {company_name}...")
            personalization["custom_extractions"] = self.custom_extraction(about_text)

        base_result["personalization"] = personalization

        primary_leadership_page = base_result.get("primary_leadership_page") or base_result.get("accepted_website")
        if primary_leadership_page:
            print(f"Verifying leadership gender split on {primary_leadership_page}...")
            base_result["scraped_leadership_audit"] = self.verify_leadership_page(
                primary_leadership_page,
                referer=base_result["accepted_website"],
            )

        return base_result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Superpowered Crawler (Final): disguise + politeness + robots + cache.")
    parser.add_argument("--input", required=True, help="Input CSV with companies")
    parser.add_argument("--output-prefix", required=True, help="Output file prefix")
    parser.add_argument("--name-col", required=True, help="Column containing company name")
    parser.add_argument("--website-col", default="", help="Optional column containing website")
    parser.add_argument("--max-pages", type=int, default=5, help="Max pages to crawl per site")
    parser.add_argument("--config", default="", help="Optional path to config.json")

    # New flags for the four upgrades.
    parser.add_argument("--rps", type=float, default=1.0,
                        help="Max requests per second per host (default 1.0).")
    parser.add_argument("--polite-delay", type=float, default=0.5,
                        help="Extra jittered sleep after each fetch in seconds (default 0.5).")
    parser.add_argument("--ignore-robots", action="store_true",
                        help="Do NOT respect robots.txt (not recommended).")
    parser.add_argument("--no-cache", action="store_true",
                        help="Disable on-disk page cache.")
    parser.add_argument("--cache-dir", default=".crawler_cache",
                        help="Directory for the disk cache (default .crawler_cache).")
    parser.add_argument("--cache-ttl", type=int, default=7 * 24 * 3600,
                        help="Cache entry lifetime in seconds (default 7 days).")
    parser.add_argument("--identify", action="store_true",
                        help="Use an honest, contactable User-Agent instead of rotating browser UAs.")
    parser.add_argument("--contact-url", default="",
                        help="Contact URL embedded in the identifying User-Agent.")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of companies to crawl in parallel (default 4). "
                             "Per-host rate limit still applies across threads.")
    parser.add_argument("--resume", action="store_true",
                        help="Skip companies already present in the output checkpoint file.")
    parser.add_argument("--no-csv", action="store_true",
                        help="Disable CSV exports (only write the JSON).")
    parser.add_argument("--country", default="Australia",
                        help="Target country filter (e.g. Australia, United Kingdom, US, Saudi Arabia).")

    args = parser.parse_args()

    config = {}
    if args.config and Path(args.config).exists():
        with open(args.config, "r") as f:
            config = json.load(f)
            print(f"Loaded custom configuration from {args.config}")

    crawler = SuperpoweredCrawlerFinal(
        config=config,
        max_pages=args.max_pages,
        use_search=True,
        polite_delay=args.polite_delay,
        rps=args.rps,
        cache_dir=args.cache_dir,
        cache_enabled=not args.no_cache,
        cache_ttl=args.cache_ttl,
        respect_robots=not args.ignore_robots,
        identify=args.identify,
        contact_url=args.contact_url,
        country_hint=args.country,
    )

    print(
        f"[mode] curl_cffi={'on' if CURL_CFFI_AVAILABLE else 'OFF (urllib)'}  "
        f"playwright={'on' if PLAYWRIGHT_AVAILABLE else 'off'}  "
        f"pdf={'on' if PYPDF_AVAILABLE else 'off'}  "
        f"rps={args.rps}/host  robots={'respect' if not args.ignore_robots else 'IGNORE'}  "
        f"cache={'off' if args.no_cache else args.cache_dir}  "
        f"identify={'yes' if args.identify else 'no'}  "
        f"workers={args.workers}  resume={'yes' if args.resume else 'no'}"
    )

    with open(args.input, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    out_file = f"{args.output_prefix}_deep_enriched.json"
    checkpoint_file = f"{args.output_prefix}_checkpoint.json"

    # Resume: load any companies we've already processed in a previous run.
    results: list[dict] = []
    done_names: set[str] = set()
    if args.resume and Path(checkpoint_file).exists():
        try:
            results = json.loads(Path(checkpoint_file).read_text("utf-8"))
            done_names = {r.get("company_name", "") for r in results}
            print(f"[resume] {len(done_names)} companies already done, skipping")
        except Exception as exc:  # noqa: BLE001
            print(f"[resume] failed to read checkpoint ({exc}); starting fresh")

    pending = [r for r in rows if r[args.name_col] not in done_names]
    print(f"[plan] {len(pending)} to crawl, {len(done_names)} cached, workers={args.workers}")

    def _process(row: dict) -> dict:
        name = row[args.name_col]
        website_hint = row.get(args.website_col, "")
        print(f"\n[start] {name}")
        try:
            return crawler.crawl_company_deep(
                company_name=name,
                row_id=row.get("row_id", name),
                website_hint=website_hint,
            )
        except Exception as exc:  # noqa: BLE001 - one bad company shouldn't kill the run
            print(f"[error] {name}: {type(exc).__name__}: {exc}")
            return {"company_name": name, "error": f"{type(exc).__name__}: {exc}"}

    # Per-company concurrency. Per-host rate limiter still serializes same-host requests.
    completed = 0
    checkpoint_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as ex:
        future_to_row = {ex.submit(_process, row): row for row in pending}
        for fut in as_completed(future_to_row):
            res = fut.result()
            # Make sure company_name is in the result so resume works
            if "company_name" not in res:
                res["company_name"] = future_to_row[fut][args.name_col]
            results.append(res)
            completed += 1
            # Checkpoint every 5 completions (cheap insurance against crashes)
            if completed % 5 == 0:
                with checkpoint_lock:
                    Path(checkpoint_file).write_text(json.dumps(results, indent=2), "utf-8")
                print(f"[checkpoint] {completed}/{len(pending)} done")

    # Final write
    Path(out_file).write_text(json.dumps(results, indent=2), "utf-8")
    Path(checkpoint_file).write_text(json.dumps(results, indent=2), "utf-8")
    print(f"\nSaved local deep enrichment results to {out_file}")

    # CSV exports: one row per company + one row per person.
    if not args.no_csv:
        companies_csv = f"{args.output_prefix}_companies.csv"
        people_csv = f"{args.output_prefix}_people.csv"
        _write_companies_csv(results, companies_csv)
        _write_people_csv(results, people_csv)
        print(f"Saved {companies_csv}")
        print(f"Saved {people_csv}")


def _write_companies_csv(results: list[dict], path: str) -> None:
    cols = [
        "company_name", "accepted_website", "primary_leadership_page",
        "contact_email", "contact_phone", "linkedin_url",
        "scraped_board_total", "scraped_board_women", "scraped_board_men",
        "scraped_exec_total", "scraped_exec_women", "scraped_exec_men",
        "people_count", "verified_email_count",
        "value_prop", "hook", "error",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in results:
            audit = r.get("scraped_leadership_audit") or {}
            pers = r.get("personalization") or {}
            people = r.get("people") or []
            verified = sum(
                1 for p in people
                if isinstance(p.get("verified_email"), str)
                and "@" in p["verified_email"]
                and not p["verified_email"].lower().startswith(("invalid", "unverified"))
            )
            w.writerow({
                "company_name": r.get("company_name", ""),
                "accepted_website": r.get("accepted_website", ""),
                "primary_leadership_page": r.get("primary_leadership_page", ""),
                "contact_email": r.get("contact_email", ""),
                "contact_phone": r.get("contact_phone", ""),
                "linkedin_url": r.get("linkedin_url", ""),
                "scraped_board_total": audit.get("scraped_board_total", ""),
                "scraped_board_women": audit.get("scraped_board_women", ""),
                "scraped_board_men": audit.get("scraped_board_men", ""),
                "scraped_exec_total": audit.get("scraped_exec_total", ""),
                "scraped_exec_women": audit.get("scraped_exec_women", ""),
                "scraped_exec_men": audit.get("scraped_exec_men", ""),
                "people_count": len(people),
                "verified_email_count": verified,
                "value_prop": pers.get("value_prop", ""),
                "hook": pers.get("hook", ""),
                "error": r.get("error", ""),
            })


def _write_people_csv(results: list[dict], path: str) -> None:
    cols = [
        "company_name", "name", "role",
        "verified_email", "verification_source",
        "guessed_emails", "linkedin_url", "source",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in results:
            company = r.get("company_name", "")
            for p in r.get("people") or []:
                w.writerow({
                    "company_name": company,
                    "name": p.get("name", ""),
                    "role": p.get("role", ""),
                    "verified_email": p.get("verified_email", ""),
                    "verification_source": p.get("verification_source", ""),
                    "guessed_emails": ";".join(p.get("guessed_emails") or []),
                    "linkedin_url": p.get("linkedin_url", ""),
                    "source": p.get("source", "ddg_xray"),
                })


if __name__ == "__main__":
    main()
