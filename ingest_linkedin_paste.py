#!/usr/bin/env python3
"""Convert pasted LinkedIn-style job alerts into job_alerts_raw.csv format."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


FIELDNAMES = [
    "company",
    "role_title",
    "location",
    "job_url",
    "job_text",
    "required_skills",
    "preferred_skills",
    "notes",
]

NOISE_PATTERNS = [
    r"school alumni work here",
    r"company alumni works here",
    r"connection works here",
    r"connections work here",
    r"Actively reviewing applicants",
    r"Be an early applicant",
    r"Viewed",
    r"Easy Apply",
    r"Posted \d+",
    r"How promoted jobs are ranked",
]

LOCATION_RE = re.compile(
    r"(?:,?\s*(?:VIC|NSW|QLD|WA|SA|ACT|NT|TAS)\b.*|\bAustralia\b.*|\bRemote\b.*|\bHybrid\b.*|\bOn-site\b.*|\bOn site\b.*)$",
    re.IGNORECASE,
)


def clean(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value


def is_noise(line: str) -> bool:
    line = clean(line)
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in NOISE_PATTERNS)


def dedupe_title(title: str) -> str:
    title = clean(title)
    title = re.sub(r"^Selected,\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\(Verified job\)", "", title, flags=re.IGNORECASE)
    for size in range(len(title) // 2, 3, -1):
        left = title[:size].strip(" -")
        right = title[size:].strip(" -")
        if left and left == right:
            return left
    m = re.match(r"(.+?)\1$", title)
    if m:
        return clean(m.group(1))
    return title


def parse_clean_lines(text: str) -> list[dict]:
    rows = []
    for raw in text.splitlines():
        line = clean(re.sub(r"^\d+\.\s*", "", raw))
        if not line or "|" not in line:
            continue
        parts = [clean(part) for part in line.split("|")]
        if len(parts) < 3:
            continue
        rows.append({
            "company": parts[0],
            "role_title": parts[1],
            "location": parts[2],
            "job_url": parts[3] if len(parts) > 3 else "",
            "job_text": "",
            "required_skills": "",
            "preferred_skills": "",
            "notes": "",
        })
    return rows


def parse_noisy_blocks(text: str) -> list[dict]:
    lines = [clean(line) for line in text.splitlines()]
    lines = [line for line in lines if line and not is_noise(line)]
    rows = []
    for idx, line in enumerate(lines):
        if not LOCATION_RE.search(line):
            continue
        if idx < 2:
            continue
        company = lines[idx - 1]
        title = dedupe_title(lines[idx - 2])
        if len(title) < 3 or len(company) < 2:
            continue
        rows.append({
            "company": company,
            "role_title": title,
            "location": line,
            "job_url": "",
            "job_text": "",
            "required_skills": "",
            "preferred_skills": "",
            "notes": "",
        })
    return rows


def dedupe_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for row in rows:
        key = tuple(clean(row.get(field, "")).lower() for field in ("company", "role_title", "location"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def parse_text(text: str) -> list[dict]:
    rows = parse_clean_lines(text)
    if not rows:
        rows = parse_noisy_blocks(text)
    return dedupe_rows(rows)


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert pasted LinkedIn alert text into raw pipeline CSV.")
    parser.add_argument("--input", required=True, help="Paste text file or clean Company|Role|Location file")
    parser.add_argument("--output", required=True, help="CSV output path")
    args = parser.parse_args()

    text = Path(args.input).read_text(encoding="utf-8")
    rows = parse_text(text)
    write_csv(Path(args.output), rows)
    print(f"[*] Parsed {len(rows)} job row(s)")
    print(f"[*] Wrote {args.output}")


if __name__ == "__main__":
    main()
