#!/usr/bin/env python3
"""
email_pattern_resolver.py

Replaces the broken search-engine-dependent resolve_company_email_pattern()
in superpowered_crawler_final.py. That method uses search_web() which calls
DuckDuckGo / Brave / Bing — all blocked on this ISP.

Four-tier fallback chain (cheapest/unlimited first):
  1. email-format.com scrape   — free, unlimited, direct HTTP, no auth
  2. Hunter.io domain-search   — 25/month, confirmed pattern + real names
  3. SMTP brute force          — free, definitive, needs ≥1 real name
  4. GitHub commit-email scan  — free, only useful for tech/eng companies

Usage:
  # Standalone — resolve patterns for all GUESS entries in company_domains.json
  python3 email_pattern_resolver.py

  # Or pass specific companies:
  python3 email_pattern_resolver.py --companies "Estia Health" "HESTA" "EnergyAustralia"

  # With a known name to unlock SMTP-brute tier:
  python3 email_pattern_resolver.py --companies "Estia Health" --names "Estia Health:John Smith"

Integration:
  from email_pattern_resolver import resolve_pattern_for_company
  # call instead of crawler.resolve_company_email_pattern(company, domain)
  pat, source = resolve_pattern_for_company(crawler, company, domain, names=[])
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

# Seven canonical patterns (in try-order for SMTP brute)
PATTERNS = [
    "{first}.{last}@{domain}",
    "{first}{last[0]}@{domain}",
    "{first[0]}{last}@{domain}",
    "{first[0]}.{last}@{domain}",
    "{first}.{last[0]}@{domain}",
    "{first}{last}@{domain}",
    "{first}@{domain}",
]

# Hunter.io pattern-field shorthand → our format strings
_HUNTER_MAP = {
    "first.last":  "{first}.{last}@{domain}",
    "first":       "{first}@{domain}",
    "firstlast":   "{first}{last}@{domain}",
    "flast":       "{first[0]}{last}@{domain}",
    "firstl":      "{first}{last[0]}@{domain}",
    "first.l":     "{first}.{last[0]}@{domain}",
    "f.last":      "{first[0]}.{last}@{domain}",
    "f_last":      "{first[0]}{last}@{domain}",
}


# ---------------------------------------------------------------------------
# Source 1 — email-format.com
# ---------------------------------------------------------------------------

def _parse_email_format_dot_com(html: str, domain: str) -> str | None:
    """
    Parse the email pattern from an email-format.com domain page.
    The page shows a table row like: john.doe@company.com
    or a masked sample: j***.d**@company.com
    Try to infer the format string from any shown sample.
    """
    if not html or "not found" in html.lower()[:500]:
        return None

    # Look for an unmasked sample email with the target domain
    sample_m = re.search(
        r"([a-z0-9._+-]+)@" + re.escape(domain),
        html, re.IGNORECASE,
    )
    if sample_m:
        local = sample_m.group(1).lower()
        return _infer_pattern_from_sample(local, domain)

    # Look for a masked sample: j***.l***@domain or j***doe@domain
    masked_m = re.search(
        r"([a-z][*a-z0-9._-]*)@" + re.escape(domain),
        html, re.IGNORECASE,
    )
    if masked_m:
        local = masked_m.group(1).lower()
        return _infer_pattern_from_sample(local, domain)

    # Last-resort: look for a visible label like "Format: first.last"
    label_m = re.search(
        r"format[:\s]+([a-z{}._ \[\]]+@[a-z.]+)",
        html, re.IGNORECASE,
    )
    if label_m:
        raw = label_m.group(1).strip()
        # already a format string?
        if "{first}" in raw or "{last}" in raw:
            return raw if domain in raw else raw.replace("@", f"@{domain}").split("@{domain}")[0] + f"@{{domain}}"
    return None


def _infer_pattern_from_sample(local: str, domain: str) -> str | None:
    """
    Given a local-part like 'john.doe', 'jdoe', 'john', infer the format string.
    Uses placeholder heuristics — works for all 7 common patterns.
    """
    # Strip asterisks (masking) and infer from visible structure
    visible = local.replace("*", "")

    # first.last  (contains a dot that's not first or last char)
    if re.match(r"^[a-z]+\.[a-z]+$", visible):
        return "{first}.{last}@{domain}"
    # f.last
    if re.match(r"^[a-z]\.[a-z]{2,}$", visible):
        return "{first[0]}.{last}@{domain}"
    # first.l
    if re.match(r"^[a-z]{2,}\.[a-z]$", visible):
        return "{first}.{last[0]}@{domain}"
    # flast (1 leading char + rest looks like a surname)
    if re.match(r"^[a-z][a-z]{2,}$", visible) and len(visible) <= 8:
        return "{first[0]}{last}@{domain}"
    # firstl (name + 1 trailing char, short)
    if re.match(r"^[a-z]{3,}[a-z]$", visible) and len(visible) <= 7:
        return "{first}{last[0]}@{domain}"
    # first only
    if re.match(r"^[a-z]{2,}$", visible) and len(visible) <= 8:
        return "{first}@{domain}"
    # firstlast (long string, no separator)
    if re.match(r"^[a-z]{5,}$", visible):
        return "{first}{last}@{domain}"
    return None


def _source_email_format(crawler, domain: str) -> tuple[str | None, str]:
    url = f"https://www.email-format.com/d/{domain}/"
    try:
        r = crawler.fetch(url)
        if r.status == 200 and r.text:
            pat = _parse_email_format_dot_com(r.text, domain)
            if pat:
                return pat, "email-format.com"
    except Exception as e:
        print(f"  [email-format] fetch failed: {e}")
    return None, ""


# ---------------------------------------------------------------------------
# Source 2 — Hunter.io domain-search API
# ---------------------------------------------------------------------------

def _hunter_domain_search(domain: str, api_key: str) -> dict | None:
    url = (
        "https://api.hunter.io/v2/domain-search?"
        + urllib.parse.urlencode({"domain": domain, "api_key": api_key, "limit": 10})
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return data.get("data") or {}
    except Exception as e:
        print(f"  [hunter] API call failed: {e}")
        return None


def _source_hunter(domain: str) -> tuple[str | None, str, list[str]]:
    """Returns (pattern, source_label, names_list). names_list is a bonus for SMTP tier."""
    api_key = os.environ.get("HUNTER_API_KEY", "")
    if not api_key:
        return None, "", []

    data = _hunter_domain_search(domain, api_key)
    if not data:
        return None, "", []

    hunter_pat = data.get("pattern", "")
    pat = _HUNTER_MAP.get(hunter_pat.lower().strip())

    # Harvest real names (bonus: feed SMTP-brute and people list)
    names = []
    for entry in data.get("emails", []):
        fn = entry.get("first_name", "")
        ln = entry.get("last_name", "")
        if fn and ln:
            names.append(f"{fn} {ln}")

    if pat:
        return pat, "hunter.io", names
    return None, "", names  # still return names even if no pattern


# ---------------------------------------------------------------------------
# Source 3 — SMTP brute force (needs ≥1 real name)
# ---------------------------------------------------------------------------

def _source_smtp_brute(crawler, domain: str, names: list[str]) -> tuple[str | None, str]:
    if not names:
        return None, ""

    # Check for catch-all first — brute force is useless on catch-all servers
    fake = f"zz-fake-test-99x@{domain}"
    if crawler.active_smtp_ping(fake, domain) == "Valid":
        print(f"  [smtp-brute] {domain} is catch-all — skipping")
        return None, ""

    # Take the first available name and try all 7 patterns
    name_parts = names[0].lower().split()
    if len(name_parts) < 2:
        return None, ""
    first = re.sub(r"[^a-z]", "", name_parts[0])
    last = re.sub(r"[^a-z]", "", name_parts[-1])

    for pat in PATTERNS:
        try:
            guess = crawler.format_email_guess(pat, first, last, domain)
            result = crawler.active_smtp_ping(guess, domain)
            if result == "Valid":
                print(f"  [smtp-brute] confirmed: {pat} (via {guess})")
                return pat, "smtp-brute"
        except Exception:
            continue
    return None, ""


# ---------------------------------------------------------------------------
# Source 4 — GitHub commit-email scan
# ---------------------------------------------------------------------------

def _source_github(domain: str) -> tuple[str | None, str]:
    url = (
        "https://api.github.com/search/commits?"
        + urllib.parse.urlencode({"q": f"author-email:*@{domain}", "per_page": 10})
    )
    headers = {
        "Accept": "application/vnd.github.cloak-preview",
        "User-Agent": "job-hunt-pipeline/1.0",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None, ""

    emails = []
    for item in (data.get("items") or []):
        email = (item.get("commit") or {}).get("author", {}).get("email", "")
        if email.endswith(f"@{domain}"):
            emails.append(email.split("@")[0].lower())

    if not emails:
        return None, ""

    # Infer dominant pattern from all samples
    from collections import Counter
    patterns_found = []
    for local in emails:
        p = _infer_pattern_from_sample(local, domain)
        if p:
            patterns_found.append(p)

    if patterns_found:
        winner = Counter(patterns_found).most_common(1)[0][0]
        return winner, "github"
    return None, ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve_pattern_for_company(
    crawler,
    company_name: str,
    domain: str,
    names: list[str] | None = None,
) -> tuple[str | None, str]:
    """
    Run the 4-tier fallback chain. Returns (pattern_string, source_label).
    pattern_string is in the {first}.{last}@{domain} format used by the pipeline.
    source_label is one of: "email-format.com", "hunter.io", "smtp-brute", "github", "".

    Drop-in replacement for crawler.resolve_company_email_pattern(company, domain).
    """
    names = names or []
    print(f"  [pattern-resolver] {company_name} / {domain}")

    # 1. email-format.com — free, unlimited
    pat, src = _source_email_format(crawler, domain)
    if pat:
        print(f"  [pattern-resolver] resolved via {src}: {pat}")
        return pat, src

    # 2. Hunter.io — 25/month, also harvests names as a side-effect
    pat, src, hunter_names = _source_hunter(domain)
    if hunter_names:
        names = names + [n for n in hunter_names if n not in names]
    if pat:
        print(f"  [pattern-resolver] resolved via {src}: {pat}")
        return pat, src

    # 3. SMTP brute on known names (definitive, needs ≥1 name)
    pat, src = _source_smtp_brute(crawler, domain, names)
    if pat:
        return pat, src

    # 4. GitHub commit emails (tech cos only)
    pat, src = _source_github(domain)
    if pat:
        print(f"  [pattern-resolver] resolved via {src}: {pat}")
        return pat, src

    print(f"  [pattern-resolver] no pattern found for {domain}")
    return None, ""


# ---------------------------------------------------------------------------
# Standalone runner — resolves all GUESS entries in company_domains.json
# ---------------------------------------------------------------------------

def _load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            pass
    return {}


def _save_cache(path: Path, cache: dict) -> None:
    path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), "utf-8")


def run_batch(company_names: list[str] | None = None, names_by_company: dict | None = None) -> None:
    from superpowered_crawler_final import SuperpoweredCrawlerFinal
    from dotenv_loader import load_env
    load_env()

    cache_path = Path("company_domains.json")
    cache = _load_cache(cache_path)

    # Default: process every entry still marked as GUESS
    if not company_names:
        company_names = [
            name for name, entry in cache.items()
            if "GUESS" in entry.get("_note", "") or not entry.get("pattern_source")
        ]

    if not company_names:
        print("Nothing to resolve — all entries already confirmed.")
        return

    print(f"Resolving patterns for {len(company_names)} companies...\n")

    # Guard: if we're about to burn many Hunter credits, warn
    hunter_key = os.environ.get("HUNTER_API_KEY", "")
    if hunter_key:
        print(f"Hunter.io key present — will use up to {len(company_names)} credits if needed.\n")

    crawler = SuperpoweredCrawlerFinal(respect_robots=True, rps=0.5, polite_delay=2.0)
    hunter_used = 0

    for company in company_names:
        entry = cache.get(company, {})
        domain = entry.get("domain", "")
        if not domain:
            print(f"  [{company}] no domain in cache — skipping")
            continue

        extra_names = (names_by_company or {}).get(company, [])
        pat, src = resolve_pattern_for_company(crawler, company, domain, names=extra_names)

        # Guard: stop if Hunter is being used too aggressively (email-format is silently failing)
        if src == "hunter.io":
            hunter_used += 1
            if hunter_used > 5:
                print(
                    "\n[WARN] Hunter used >5 times in this run — email-format.com may be failing silently. "
                    "Check email-format parse logic before continuing.\n"
                )

        if pat:
            cache.setdefault(company, {})
            cache[company]["domain"] = domain
            cache[company]["pattern"] = pat
            cache[company]["pattern_source"] = src
            cache[company]["pattern_confirmed_at"] = date.today().isoformat()
            cache[company]["_note"] = f"confirmed via {src}"
            _save_cache(cache_path, cache)
            print(f"  ✓  {company}: {pat} (source: {src})\n")
        else:
            print(f"  ✗  {company}: unresolved — manual check needed\n")

    print(f"Done. Cache updated at {cache_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Resolve email patterns for target companies.")
    ap.add_argument(
        "--companies", nargs="*",
        help="Company names to resolve (default: all GUESS entries in company_domains.json)",
    )
    ap.add_argument(
        "--names", nargs="*", metavar="COMPANY:Full Name",
        help='Known names to unlock SMTP-brute tier, e.g. "HESTA:Jane Smith"',
    )
    args = ap.parse_args()

    names_by_company: dict[str, list[str]] = {}
    for item in (args.names or []):
        if ":" in item:
            company, name = item.split(":", 1)
            names_by_company.setdefault(company.strip(), []).append(name.strip())

    run_batch(args.companies, names_by_company)


if __name__ == "__main__":
    main()
