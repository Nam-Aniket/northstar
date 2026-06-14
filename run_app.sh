#!/usr/bin/env bash
# Launch the Job-Hunt Console (Northstar) locally.
# NOTE: no --reload. The pipeline writes data files (run_status.json, the DB,
# resumes/) inside this tree; --reload would watch those and restart the server
# on every write, making the UI flicker/reload constantly. Templates and CSS are
# re-read per request anyway, so the app stays current. After a code (.py) change,
# restart this script to pick it up.
cd "$(dirname "$0")"
echo "Northstar → http://127.0.0.1:8765"
exec .venv/bin/uvicorn app.app:app --host 127.0.0.1 --port 8765
