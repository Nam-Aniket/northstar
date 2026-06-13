#!/usr/bin/env python3
"""
Job Outreach Pipeline.

Wraps superpowered_crawler_final.SuperpoweredCrawlerFinal to turn a
"reachout data" CSV (one row per company with up to 3 LinkedIn URLs)
into a fully enriched job-outreach dataset:

  - Resolves each company's website (TLS-disguise, robots-respecting fetch).
  - Multi-role LinkedIn x-ray: runs a SEPARATE DuckDuckGo query per role
    category (Data / Talent / Hiring Leadership) so we catch role-name
    variation across companies.
  - Extracts name + role from the LinkedIn URLs you already had in the
    Person 1/2/3 columns and merges them into the people list.
  - Guesses 3 email patterns per person and verifies via SMTP ping +
    DuckDuckGo "does this email appear online" fallback for catch-all hosts.
  - Writes job-outreach-ready CSVs (one row per person, plus a per-company
    summary), preserving the "why this company" rationale on every row.

Usage:
  python3 job_outreach_pipeline.py \\
    --input "data/reachout_data.csv" \\
    --output-prefix job_outreach \\
    --config job_outreach_config.json \\
    --workers 4 --resume
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from superpowered_crawler_final import (
    SuperpoweredCrawlerFinal,
    CURL_CFFI_AVAILABLE,
    PLAYWRIGHT_AVAILABLE,
    PYPDF_AVAILABLE,
)
from local_apify_crawler import clean_text


# ---------------------------------------------------------------------------
# Name normalisation + LinkedIn URL parsing
# ---------------------------------------------------------------------------

LINKEDIN_SLUG_RE = re.compile(r"linkedin\.com/in/([^/?#]+)")


def normalize_name(name: str) -> str:
    """Lowercase, strip, collapse whitespace, remove non-letters - for dedup keys."""
    if not name:
        return ""
    return re.sub(r"[^a-z]+", "", name.lower())


def name_from_linkedin_slug(url: str) -> str:
    """
    Best-effort: 'linkedin.com/in/jordan-rivera/' -> 'Jordan Rivera'.

    Strips numeric/hash suffixes ('-a19984171', '-2217b035') that LinkedIn
    appends when a name slug collides.
    """
    m = LINKEDIN_SLUG_RE.search(url or "")
    if not m:
        return ""
    slug = m.group(1)
    # Drop trailing hash-y suffix segments (>=6 chars mixed alnum, with at least one digit)
    parts = [p for p in slug.split("-") if not (len(p) >= 6 and any(c.isdigit() for c in p) and any(c.isalpha() for c in p))]
    # Also drop pure-digit tails
    parts = [p for p in parts if not p.isdigit()]
    if not parts:
        parts = slug.split("-")
    return " ".join(p.capitalize() for p in parts)


def dedupe_people(people: list[dict]) -> list[dict]:
    """Merge entries that share the same normalised name. Prefer the richest record."""
    by_key: dict[str, dict] = {}
    for p in people:
        key = normalize_name(p.get("name", ""))
        if not key:
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = dict(p)
            continue
        # Merge: prefer non-empty fields
        for k, v in p.items():
            if not existing.get(k) and v:
                existing[k] = v
        # Track all sources
        sources = set([existing.get("source", "")] + [p.get("source", "")])
        existing["source"] = ",".join(sorted(s for s in sources if s))
    return list(by_key.values())


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

class JobOutreachPipeline:
    def __init__(self, crawler: SuperpoweredCrawlerFinal, role_categories: dict[str, list[str]]):
        self.crawler = crawler
        self.role_categories = role_categories

    # ----- step 1: multi-role x-ray --------------------------------------

    def multi_role_xray(self, company_name: str) -> list[dict]:
        """Run one DuckDuckGo LinkedIn x-ray per role category. Merge + tag."""
        all_people: list[dict] = []
        for category, roles in self.role_categories.items():
            # Set roles on the crawler config for this call (xray_linkedin reads from self.config).
            self.crawler.config["target_roles"] = roles
            try:
                hits = self.crawler.xray_linkedin(company_name)
            except Exception as exc:  # noqa: BLE001
                print(f"  [xray:{category}] {company_name}: {type(exc).__name__}: {exc}")
                hits = []
            for h in hits:
                h["role_bucket"] = category
                h["source"] = f"ddg_xray:{category}"
            all_people.extend(hits)
            print(f"  [xray:{category}] {company_name}: {len(hits)} hits")
        return all_people

    # ----- step 2: enrich the LinkedIn URLs already in the CSV -----------

    def enrich_seed_urls(self, seed_urls: list[str]) -> list[dict]:
        """Turn raw LinkedIn URLs from the input CSV into name+url entries."""
        out: list[dict] = []
        for url in seed_urls:
            if not url or "linkedin.com/in/" not in url:
                continue
            name = name_from_linkedin_slug(url)
            if not name:
                continue
            out.append({
                "name": name,
                "role": "(from seed CSV)",
                "linkedin_url": url.strip(),
                "role_bucket": "seed",
                "source": "seed_csv",
            })
        return out

    # ----- step 3: full per-company flow ----------------------------------

    def process_company(self, row: dict) -> dict:
        company = row.get("Company", "").strip()
        role_target = row.get("Role Type", "").strip()
        rationale = row.get("Why this Company?", "").strip()
        seed_urls = [row.get("Person 1", ""), row.get("Person 2", ""), row.get("Person 3", "")]

        print(f"\n=== {company} ===")

        # 1. Resolve company website via base crawler (this also fills contact_email/phone).
        try:
            base = self.crawler.crawl_company(company_name=company, row_id=company, website_hint="")
        except Exception as exc:  # noqa: BLE001
            print(f"  [crawl] error: {type(exc).__name__}: {exc}")
            base = {}

        website = base.get("accepted_website", "")
        domain = urllib.parse.urlparse(website).netloc.replace("www.", "") if website else ""

        rejected_reasons = [rd.get("reason", "") for rd in base.get("rejected_domains", [])]
        is_out_of_sector = any("out of sector" in r for r in rejected_reasons)

        if is_out_of_sector:
            status = "rejected: out of sector"
            print(f"  [status] Skipping LinkedIn X-ray & Email guessing: Company is out of sector.")
        elif not website:
            status = "rejected: missing website"
            print(f"  [status] Skipping LinkedIn X-ray & Email guessing: No valid website found.")
        else:
            status = "accepted"

        # 2. Multi-role x-ray (data + talent + hiring leadership).
        xray_people = self.multi_role_xray(company) if (company and status == "accepted") else []

        # 3. Seed-URL people from the input CSV.
        seed_people = self.enrich_seed_urls(seed_urls) if status == "accepted" else []

        # 4. Merge + dedupe.
        all_people = dedupe_people(xray_people + seed_people)
        if status == "accepted":
            print(f"  [merge] {len(all_people)} unique people ({len(seed_people)} seed + {len(xray_people)} xray)")

        # 5. Email guess + verify (uses the crawler's all-3-patterns + DDG fallback).
        if status == "accepted" and domain:
            all_people = self.crawler.guess_and_verify_emails_locally(all_people, domain, company_name=company)

        return {
            "company_name": company,
            "role_target": role_target,
            "rationale": rationale,
            "accepted_website": website,
            "domain": domain,
            "contact_email": base.get("contact_email", ""),
            "contact_phone": base.get("contact_phone", ""),
            "linkedin_url": base.get("linkedin_url", ""),
            "people": all_people,
            "people_count": len(all_people),
            "status": status,
            "verified_email_count": sum(
                1 for p in all_people
                if isinstance(p.get("verified_email"), str)
                and "@" in p["verified_email"]
                and not p["verified_email"].lower().startswith(("invalid", "unverified"))
            ),
        }


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def write_people_csv(results: list[dict], path: str) -> None:
    cols = [
        "company_name", "role_target", "rationale",
        "name", "role", "role_bucket",
        "verified_email", "verification_source",
        "guessed_emails", "linkedin_url",
        "domain", "accepted_website", "source",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in results:
            if r.get("status", "accepted") != "accepted":
                continue
            for p in r.get("people") or []:
                w.writerow({
                    "company_name": r.get("company_name", ""),
                    "role_target": r.get("role_target", ""),
                    "rationale": r.get("rationale", ""),
                    "name": p.get("name", ""),
                    "role": p.get("role", ""),
                    "role_bucket": p.get("role_bucket", ""),
                    "verified_email": p.get("verified_email", ""),
                    "verification_source": p.get("verification_source", ""),
                    "guessed_emails": ";".join(p.get("guessed_emails") or []),
                    "linkedin_url": p.get("linkedin_url", ""),
                    "domain": r.get("domain", ""),
                    "accepted_website": r.get("accepted_website", ""),
                    "source": p.get("source", ""),
                })


def write_companies_csv(results: list[dict], path: str) -> None:
    cols = [
        "company_name", "role_target", "rationale",
        "accepted_website", "domain",
        "contact_email", "contact_phone", "linkedin_url",
        "people_count", "verified_email_count",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in results:
            if r.get("status", "accepted") != "accepted":
                continue
            w.writerow({k: r.get(k, "") for k in cols})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Job Outreach Pipeline (wraps superpowered_crawler_final).")
    ap.add_argument("--input", required=True, help="Reachout CSV with Company + Person 1/2/3 columns.")
    ap.add_argument("--output-prefix", required=True, help="Output file prefix.")
    ap.add_argument("--config", default="job_outreach_config.json",
                    help="Role taxonomy + custom regex rules.")
    ap.add_argument("--rps", type=float, default=1.0)
    ap.add_argument("--polite-delay", type=float, default=0.5)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--ignore-robots", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--identify", action="store_true")
    ap.add_argument("--contact-url", default="https://example.invalid/job-outreach")
    ap.add_argument("--country", default="Australia", help="Target country filter (e.g. Australia, United Kingdom, Saudi Arabia, United States)")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    config = json.loads(cfg_path.read_text("utf-8")) if cfg_path.exists() else {}
    role_categories = config.get("role_categories") or {
        "data": ["Data Analyst", "Data Scientist", "Data Engineer", "BI Analyst"],
        "talent": ["Recruiter", "Talent Acquisition", "Head of People"],
    }

    crawler = SuperpoweredCrawlerFinal(
        config=config,
        max_pages=args.max_pages,
        use_search=True,
        rps=args.rps,
        polite_delay=args.polite_delay,
        cache_enabled=not args.no_cache,
        respect_robots=not args.ignore_robots,
        identify=args.identify,
        contact_url=args.contact_url,
        country_hint=args.country,
    )

    print(
        f"[mode] curl_cffi={'on' if CURL_CFFI_AVAILABLE else 'off'}  "
        f"playwright={'on' if PLAYWRIGHT_AVAILABLE else 'off'}  "
        f"pdf={'on' if PYPDF_AVAILABLE else 'off'}  "
        f"rps={args.rps}/host  workers={args.workers}  "
        f"role-buckets={list(role_categories.keys())}  country={args.country}"
    )

    pipeline = JobOutreachPipeline(crawler, role_categories)

    # Load input
    with open(args.input, "r", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Company", "").strip()]
    print(f"[plan] {len(rows)} companies in {args.input}")

    # Resume
    out_json = f"{args.output_prefix}_enriched.json"
    checkpoint = f"{args.output_prefix}_checkpoint.json"
    results: list[dict] = []
    done: set[str] = set()
    if args.resume and Path(checkpoint).exists():
        try:
            results = json.loads(Path(checkpoint).read_text("utf-8"))
            done = {r.get("company_name", "") for r in results}
            print(f"[resume] {len(done)} already done, skipping")
        except Exception as exc:  # noqa: BLE001
            print(f"[resume] checkpoint unreadable ({exc}); starting fresh")

    pending = [r for r in rows if r.get("Company", "").strip() not in done]

    completed = 0
    lock = threading.Lock()

    def _run(row):
        try:
            return pipeline.process_company(row)
        except Exception as exc:  # noqa: BLE001
            print(f"[error] {row.get('Company','?')}: {type(exc).__name__}: {exc}")
            return {"company_name": row.get("Company", ""), "error": str(exc), "people": []}

    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as ex:
        futures = {ex.submit(_run, row): row for row in pending}
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            completed += 1
            if completed % 3 == 0:
                with lock:
                    Path(checkpoint).write_text(json.dumps(results, indent=2), "utf-8")
                print(f"[checkpoint] {completed}/{len(pending)}")

    Path(out_json).write_text(json.dumps(results, indent=2), "utf-8")
    Path(checkpoint).write_text(json.dumps(results, indent=2), "utf-8")

    people_csv = f"{args.output_prefix}_people.csv"
    companies_csv = f"{args.output_prefix}_companies.csv"
    write_people_csv(results, people_csv)
    write_companies_csv(results, companies_csv)

    # Summary
    total_people = sum(r.get("people_count", 0) for r in results)
    total_verified = sum(r.get("verified_email_count", 0) for r in results)
    print(
        f"\n[done] {len(results)} companies | {total_people} people | "
        f"{total_verified} verified emails"
    )
    print(f"  {out_json}")
    print(f"  {people_csv}")
    print(f"  {companies_csv}")


if __name__ == "__main__":
    main()
