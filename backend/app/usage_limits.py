"""Usage limits for the public deployment.

The site is public, so every endpoint that spends money (LLM calls, paid scraping,
research runs) gets a per-visitor budget, and the most expensive ones also get a
global daily cap. The dashboard and all precomputed data stay unrestricted.

Per-IP windows live in memory: Railway runs one replica, and a restart only resets
the hourly window. Daily caps are the cost ceiling, so they persist to a gitignored
file and survive restarts.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

HOUR = 3600.0
_DAILY_FILE = Path(__file__).resolve().parents[2] / "cache" / "agent" / "usage_daily.json"


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


# (method, path) -> (bucket, per-IP limit per hour, global limit per day or None)
COSTLY_ROUTES: dict[tuple[str, str], tuple[str, int, int | None]] = {
    ("POST", "/api/research/run"): ("research", _env_int("RESEARCH_IP_PER_HOUR", 1), _env_int("RESEARCH_DAILY_LIMIT", 3)),
    ("GET", "/api/esg-news/company"): ("esg_news", _env_int("NEWS_IP_PER_HOUR", 20), _env_int("NEWS_DAILY_LIMIT", 200)),
    ("POST", "/api/settings/scraping/test"): ("scrape_test", _env_int("SCRAPE_TEST_IP_PER_HOUR", 5), None),
}
BRIEFING_REFRESH = ("briefing_refresh", _env_int("BRIEFING_REFRESH_IP_PER_HOUR", 2), _env_int("BRIEFING_REFRESH_DAILY_LIMIT", 3))
CHAT_IP_PER_HOUR = _env_int("AGENT_IP_PER_HOUR", 20)
WRITES_IP_PER_HOUR = _env_int("WRITES_IP_PER_HOUR", 120)

_lock = threading.Lock()
_windows: dict[tuple[str, str], tuple[float, int]] = {}


def client_ip(request: Request) -> str:
    """Railway's edge appends the client address to X-Forwarded-For, so the
    rightmost entry is the one a client cannot forge."""
    forwarded = request.headers.get("x-forwarded-for", "")
    last = forwarded.split(",")[-1].strip()
    if last:
        return last
    return request.client.host if request.client else "unknown"


def allow_ip(bucket: str, ip: str, limit: int) -> bool:
    now = time.monotonic()
    with _lock:
        start, count = _windows.get((bucket, ip), (now, 0))
        if now - start >= HOUR:
            start, count = now, 0
        if count >= limit:
            return False
        _windows[(bucket, ip)] = (start, count + 1)
        if len(_windows) > 20_000:
            for key, (begun, _) in list(_windows.items()):
                if now - begun >= HOUR:
                    del _windows[key]
        return True


def allow_today(bucket: str, limit: int) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _lock:
        try:
            data = json.loads(_DAILY_FILE.read_text("utf-8")) if _DAILY_FILE.exists() else {}
        except Exception:
            data = {}
        if data.get("date") != today:
            data = {"date": today, "counts": {}}
        counts = data.setdefault("counts", {})
        if int(counts.get(bucket, 0)) >= limit:
            return False
        counts[bucket] = int(counts.get(bucket, 0)) + 1
        try:
            _DAILY_FILE.parent.mkdir(parents=True, exist_ok=True)
            _DAILY_FILE.write_text(json.dumps(data), "utf-8")
        except Exception:
            pass
        return True


def _limit_for(request: Request) -> tuple[str, int, int | None] | None:
    method, path = request.method, request.url.path
    if (method, path) in COSTLY_ROUTES:
        return COSTLY_ROUTES[(method, path)]
    if method == "GET" and path == "/api/dashboard/briefing" and request.query_params.get("refresh") in ("true", "1"):
        return BRIEFING_REFRESH
    # Chat has its own limit inside the route so the UI gets an assistant reply.
    if method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api/") and not path.startswith("/api/assistant/chat"):
        return ("writes", WRITES_IP_PER_HOUR, None)
    return None


async def usage_limit_middleware(request: Request, call_next):
    rule = _limit_for(request)
    if rule is not None:
        bucket, per_ip, per_day = rule
        if not allow_ip(bucket, client_ip(request), per_ip):
            return JSONResponse(
                {"detail": "Too many requests from this address. Please try again in an hour."},
                status_code=429,
                headers={"Retry-After": "3600"},
            )
        if per_day is not None and not allow_today(bucket, per_day):
            return JSONResponse(
                {"detail": "The shared daily limit for this action has been reached. Please try again tomorrow."},
                status_code=429,
                headers={"Retry-After": "3600"},
            )
    return await call_next(request)
