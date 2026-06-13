"""csv_merge.py — key-based upsert helpers for job pipeline CSVs."""
from __future__ import annotations

import re


def _norm(s: object) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def canonical_url(url: str) -> str:
    url = re.sub(r"\s+", " ", (url or "")).strip()
    if not url:
        return ""
    m = re.search(r"/jobs/view/(\d+)", url)
    if m:
        return f"linkedin:{m.group(1)}"
    return url.split("#", 1)[0].rstrip("/").lower()


def row_key(row: dict) -> str:
    url = canonical_url(
        row.get("job_url") or row.get("LinkedIn URL") or ""
    )
    if url:
        return url
    company = _norm(row.get("company") or row.get("Company") or "")
    title = _norm(row.get("role_title") or row.get("Job Title") or "")
    return f"ct:{company}|{title}"


_PREFER_NONBLANK = (
    "job_text",
    "required_skills",
    "preferred_skills",
    "company_domain",
    "location",
    "jd_fetch_status",
)


def merge_csv_on_key(
    existing_rows: list[dict],
    new_rows: list[dict],
    key_fn=row_key,
    prefer_nonblank: tuple[str, ...] = _PREFER_NONBLANK,
) -> list[dict]:
    """Upsert new_rows into existing_rows by key_fn.

    - New key → append.
    - Existing key → merge per-column:
        job_text: keep the longer stripped value.
        prefer_nonblank cols: keep existing if non-blank, else take new.
        all other cols: newest wins (new row).
    Returns list preserving insertion order (existing first, then new keys).
    """
    index: dict[str, int] = {}
    result: list[dict] = []
    for row in existing_rows:
        k = key_fn(row)
        if k not in index:
            index[k] = len(result)
            result.append(dict(row))
        # duplicate existing keys: last write wins
        else:
            result[index[k]] = dict(row)

    for new_row in new_rows:
        k = key_fn(new_row)
        if k not in index:
            index[k] = len(result)
            result.append(dict(new_row))
        else:
            merged = result[index[k]]
            for col, val in new_row.items():
                new_val = (val or "").strip() if isinstance(val, str) else val
                if col == "job_text":
                    existing_val = (merged.get(col) or "").strip()
                    new_stripped = (new_val or "").strip() if isinstance(new_val, str) else ""
                    if len(new_stripped) > len(existing_val):
                        merged[col] = new_stripped
                elif col in prefer_nonblank:
                    if not (merged.get(col) or "").strip():
                        merged[col] = val
                else:
                    merged[col] = val
            result[index[k]] = merged

    return result


if __name__ == "__main__":
    # Self-test: merge keeps the longer job_text
    existing = [{"job_url": "https://example.com/job/1", "job_text": "short text"}]
    new = [{"job_url": "https://example.com/job/1", "job_text": "much longer text that definitely wins the merge"}]
    merged = merge_csv_on_key(existing, new)
    assert len(merged) == 1, "Should have 1 row after merge"
    assert merged[0]["job_text"] == "much longer text that definitely wins the merge", (
        f"Expected longer text, got: {merged[0]['job_text']!r}"
    )

    # Test: new key is appended
    existing2 = [{"job_url": "https://example.com/job/1", "job_text": "a"}]
    new2 = [{"job_url": "https://example.com/job/2", "job_text": "b"}]
    merged2 = merge_csv_on_key(existing2, new2)
    assert len(merged2) == 2, "Should have 2 rows"

    # Test: prefer_nonblank preserves existing non-blank
    existing3 = [{"job_url": "https://example.com/job/3", "location": "Melbourne"}]
    new3 = [{"job_url": "https://example.com/job/3", "location": "Sydney"}]
    merged3 = merge_csv_on_key(existing3, new3)
    assert merged3[0]["location"] == "Melbourne", "prefer_nonblank should keep existing"

    print("All self-tests passed.")
