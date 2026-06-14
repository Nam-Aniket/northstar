@echo off
REM Launch the Job-Hunt Console (Northstar) locally on Windows.
REM No --reload: the pipeline writes data files inside this tree and --reload
REM would restart the server on every write. Templates/CSS still refresh per
REM request. After a code (.py) change, restart this script to pick it up.
cd /d "%~dp0"
echo Northstar -^> http://127.0.0.1:8765
".venv\Scripts\uvicorn.exe" app.app:app --host 127.0.0.1 --port 8765
