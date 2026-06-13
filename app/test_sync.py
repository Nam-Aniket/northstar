"""app/test_sync.py — Acceptance test: app_state survives a pipeline rerun.

Run:
    python app/test_sync.py          (from project root)
    python -m app.test_sync          (from project root)
"""
import os
import sys
import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app import db, sync as sync_mod


def main() -> None:
    # 1. Full sync
    con = db.connect()
    db.init_schema(con)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    sync_mod.sync(con, now)

    # Verify jobs table has rows
    job_count = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    if job_count == 0:
        print("ACCEPTANCE FAIL: jobs table is empty after first sync")
        con.close()
        sys.exit(1)

    # 2. Pick the first row_key from jobs
    first_key = con.execute("SELECT row_key FROM jobs LIMIT 1").fetchone()[0]

    # 3. Insert app_state for that key
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    con.execute(
        """INSERT OR REPLACE INTO app_state
               (row_key, applied_at, notes, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (first_key, "2026-06-13T10:00:00", "TEST", ts, ts),
    )
    con.commit()

    # 4. Simulate a pipeline rerun (Zone 1 rebuild)
    now2 = datetime.datetime.now().isoformat(timespec="seconds")
    sync_mod.sync(con, now2)

    # 5. Assert app_state row is intact
    row = con.execute(
        "SELECT * FROM app_state WHERE row_key = ?", (first_key,)
    ).fetchone()

    if row is None:
        print(f"ACCEPTANCE FAIL: app_state row for {first_key!r} was deleted during sync")
        con.close()
        sys.exit(1)

    if row["applied_at"] != "2026-06-13T10:00:00":
        print(f"ACCEPTANCE FAIL: applied_at was overwritten. Got {row['applied_at']!r}")
        con.close()
        sys.exit(1)

    if row["notes"] != "TEST":
        print(f"ACCEPTANCE FAIL: notes was overwritten. Got {row['notes']!r}")
        con.close()
        sys.exit(1)

    # 6. Assert jobs still has rows after second sync
    job_count2 = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    if job_count2 == 0:
        print("ACCEPTANCE FAIL: jobs table is empty after second sync")
        con.close()
        sys.exit(1)

    con.close()
    print("ACCEPTANCE PASS")


if __name__ == "__main__":
    main()
