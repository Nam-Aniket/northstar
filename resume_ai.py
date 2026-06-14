"""AI-assisted résumé writing helpers (stdlib only, OpenAI-compatible endpoint).

Env vars (same as 04_draft_emails.py):
  LLM_API_KEY   required.
  LLM_BASE_URL  default: https://api.deepseek.com/v1
  LLM_MODEL     default: deepseek-chat
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from dotenv_loader import load_env

load_env()

_DEFAULT_BASE = "https://api.deepseek.com/v1"
_DEFAULT_MODEL = "deepseek-chat"


def _llm(messages: list[dict], temperature: float = 0.4) -> str:
    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError("LLM_API_KEY not set")
    base_url = os.environ.get("LLM_BASE_URL", _DEFAULT_BASE).rstrip("/")
    model = os.environ.get("LLM_MODEL", _DEFAULT_MODEL)

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 600,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"LLM HTTP {exc.code}: {body}") from None
    return data["choices"][0]["message"]["content"].strip()


_SUMMARY_SYSTEM = (
    "You are an expert résumé writer. "
    "Write a 2–3 sentence professional summary in first-person-implied (no 'I'), "
    "tailored to the target role, grounded ONLY in the details/skills given "
    "(never invent employers, numbers, or tools). "
    "No clichés ('results-driven', 'team player', 'passionate'), no buzzword soup. "
    "Output ONLY the summary text."
)


def generate_summary(role: str, details: str, skills: str) -> str:
    user_msg = (
        f"Target role: {role}\n\n"
        f"Details / experience notes:\n{details}\n\n"
        f"Key skills: {skills}"
    )
    return _llm([
        {"role": "system", "content": _SUMMARY_SYSTEM},
        {"role": "user", "content": user_msg},
    ])


_BULLETS_SYSTEM = (
    "You are an expert résumé writer. "
    "Produce EXACTLY ONE résumé bullet per non-empty input line, in the SAME ORDER. "
    "Rewrite each line into a strong bullet using the XYZ formula (accomplished X, measured by Y, "
    "by doing Z), starting with a past-tense action verb, concise (<= ~30 words). "
    "CRITICAL — use ONLY the facts and numbers from THAT line. Never move a number from one line to "
    "another, never duplicate a figure across bullets, never invent a percentage, count, dollar, or "
    "time figure. If a line has no number, describe the impact qualitatively (e.g. 'reducing manual "
    "effort') — do NOT add one. Inventing or relocating a metric is a critical failure. "
    "Output ONLY the bullets, one per line, no leading dashes or numbering."
)


def improve_bullets(raw: str, role: str) -> str:
    user_msg = (
        f"Target role: {role}\n\n"
        f"Raw notes:\n{raw}"
    )
    return _llm([
        {"role": "system", "content": _BULLETS_SYSTEM},
        {"role": "user", "content": user_msg},
    ])
