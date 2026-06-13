"""fill_missing_jds.py — auto-fetch JD text for rows where it is blank.

Reads --input CSV (default job_posts_enriched.csv), skips rows that already
have a real job_text (>= JD_MIN_CHARS), fetches LinkedIn JDs via the existing
01_fetch_job_descriptions module for rows that have a /jobs/view/[0-9]+ URL, then
writes the CSV back in-place and updates the JD cache.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import sys
from glob import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent

JD_MIN_CHARS = 350

_BLANK_VALUES = {"n/a", "na", "none", "not listed", "no description", "see link", "tbd"}


def jd_is_blank(text: object) -> bool:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if len(t) < JD_MIN_CHARS:
        return True
    return t.lower() in _BLANK_VALUES


# ---------------------------------------------------------------------------
# csv_merge helpers (import from same dir)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(ROOT))
from csv_merge import canonical_url, row_key  # noqa: E402


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _load_cache(sidecar_path: Path) -> dict[str, str]:
    """Load cache from jd_cache.json + any fetched_jobs_*.json sidecars."""
    cache: dict[str, str] = {}

    def _ingest(items: list) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url", "")
            desc = (item.get("description_full") or "").strip()
            if not url or not desc:
                continue
            k = row_key({"job_url": url})
            existing = cache.get(k, "")
            if len(desc) > len(existing):
                cache[k] = desc

    # existing jd_cache.json
    if sidecar_path.exists():
        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _ingest(data)
        except Exception:
            pass

    # step-01 sidecars
    pattern = str(ROOT / "data" / "outputs" / "fetched_jobs_*.json")
    for fpath in glob(pattern):
        try:
            data = json.loads(Path(fpath).read_text(encoding="utf-8"))
            if isinstance(data, list):
                _ingest(data)
        except Exception:
            pass

    return cache


def _save_cache(sidecar_path: Path, cache: dict[str, str]) -> None:
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    items = [{"url": k, "description_full": v} for k, v in cache.items()]
    tmp = sidecar_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, sidecar_path)


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return fieldnames, rows


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Fetcher loader
# ---------------------------------------------------------------------------

def _load_fetcher_module():
    spec = importlib.util.spec_from_file_location(
        "fetch_job_descriptions",
        ROOT / "01_fetch_job_descriptions.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Auto-fetch JD text for rows where it is blank."
    )
    ap.add_argument("--input", default="job_posts_enriched.csv",
                    help="Input CSV (default: job_posts_enriched.csv)")
    ap.add_argument("--sidecar", default="data/outputs/jd_cache.json",
                    help="JD cache JSON (default: data/outputs/jd_cache.json)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Max rows to network-fetch in this run")
    ap.add_argument("--rps", type=float, default=0.2,
                    help="Requests per second for crawler (default: 0.2)")
    ap.add_argument("--delay", type=float, default=4.0,
                    help="Polite delay between requests in seconds (default: 4.0)")
    args = ap.parse_args(argv)

    input_path = ROOT / args.input
    sidecar_path = ROOT / args.sidecar

    if not input_path.exists():
        sys.exit(f"[!] Input file not found: {input_path}")

    fieldnames, rows = _read_csv(input_path)

    # Ensure output columns exist
    if "jd_fetch_status" not in fieldnames:
        fieldnames.append("jd_fetch_status")
    if "job_text" not in fieldnames:
        fieldnames.append("job_text")

    cache = _load_cache(sidecar_path)

    # Classify rows
    todo_indices: list[int] = []   # need network fetch
    statuses: dict[str, int] = {}

    for i, row in enumerate(rows):
        jt = row.get("job_text", "")
        if not jd_is_blank(jt):
            row["jd_fetch_status"] = "present"
            statuses["present"] = statuses.get("present", 0) + 1
            continue

        k = row_key(row)
        cached = cache.get(k, "")
        if cached and not jd_is_blank(cached):
            row["job_text"] = cached
            row["jd_fetch_status"] = "cache"
            statuses["cache"] = statuses.get("cache", 0) + 1
            continue

        url = row.get("job_url") or row.get("LinkedIn URL") or ""
        if "linkedin.com/jobs" in url.lower() and re.search(r"/jobs/view/\d+", url):
            todo_indices.append(i)
        else:
            # TODO: jd_resolver fallback when search keys present
            row["jd_fetch_status"] = "blank_no_fetchable_url"
            statuses["blank_no_fetchable_url"] = statuses.get("blank_no_fetchable_url", 0) + 1

    # Apply --limit
    if args.limit is not None:
        over = todo_indices[args.limit:]
        for i in over:
            rows[i]["jd_fetch_status"] = "blank_no_fetchable_url"
            statuses["blank_no_fetchable_url"] = statuses.get("blank_no_fetchable_url", 0) + 1
        todo_indices = todo_indices[: args.limit]

    # Network fetch
    if todo_indices:
        fetcher_ok = False
        mod = None
        crawler = None
        try:
            mod = _load_fetcher_module()
            crawler = mod.SuperpoweredCrawlerFinal(
                respect_robots=False, rps=args.rps, polite_delay=args.delay
            )
            fetcher_ok = True
        except Exception as exc:
            print(f"[!] Fetcher load failed ({exc}); marking rows fetch_error")
            for i in todo_indices:
                rows[i]["jd_fetch_status"] = "fetch_error"
            statuses["fetch_error"] = statuses.get("fetch_error", 0) + len(todo_indices)

        if fetcher_ok:
            jobs = []
            for i in todo_indices:
                row = rows[i]
                jobs.append({
                    "Company": row.get("company") or row.get("Company") or "",
                    "Job Title": row.get("role_title") or row.get("Job Title") or "",
                    "Job Location": row.get("location") or row.get("Job Location") or "",
                    "LinkedIn URL": row.get("job_url") or row.get("LinkedIn URL") or "",
                })

            try:
                results = mod.fetch_all(jobs, crawler)
            except Exception as exc:
                print(f"[!] fetch_all failed ({exc}); marking rows fetch_error")
                results = [{}] * len(jobs)

            for idx, (i, res) in enumerate(zip(todo_indices, results)):
                enriched = (res or {}).get("enriched") or {}
                desc = (enriched.get("description") or "").strip()
                method = (res or {}).get("method") or "unknown"
                if desc and not jd_is_blank(desc):
                    rows[i]["job_text"] = desc
                    status = f"fetched:{method}"
                    rows[i]["jd_fetch_status"] = status
                    statuses[status] = statuses.get(status, 0) + 1
                    # update cache
                    k = row_key(rows[i])
                    if len(desc) > len(cache.get(k, "")):
                        cache[k] = desc
                else:
                    rows[i]["jd_fetch_status"] = "fetch_failed_blank"
                    statuses["fetch_failed_blank"] = statuses.get("fetch_failed_blank", 0) + 1

    # Write output
    _write_csv(input_path, fieldnames, rows)
    _save_cache(sidecar_path, cache)

    summary = ", ".join(f"{k}:{v}" for k, v in sorted(statuses.items()))
    print(f"[*] fill_missing_jds complete — {summary}")
    print(f"[*] Wrote {input_path}")


if __name__ == "__main__":
    main()
