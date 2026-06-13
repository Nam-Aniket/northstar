#!/usr/bin/env bash
# Launch the Job-Hunt Console (Northstar) locally.
cd "$(dirname "$0")"
echo "Northstar → http://127.0.0.1:8765"
exec .venv/bin/uvicorn app.app:app --host 127.0.0.1 --port 8765 --reload
