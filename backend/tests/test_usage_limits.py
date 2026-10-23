"""Usage limits on the public deployment's costly endpoints."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import usage_limits


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(usage_limits, "_DAILY_FILE", tmp_path / "usage_daily.json")
    monkeypatch.setattr(usage_limits, "_windows", {})
    app = FastAPI()
    app.middleware("http")(usage_limits.usage_limit_middleware)

    @app.post("/api/research/run")
    def research():
        return {"ok": True}

    @app.get("/api/dashboard/briefing")
    def briefing(refresh: bool = False):
        return {"ok": True}

    @app.put("/api/settings/models")
    def settings():
        return {"ok": True}

    return TestClient(app)


def test_research_run_is_limited_per_ip(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ip = {"x-forwarded-for": "203.0.113.7"}
    assert client.post("/api/research/run", headers=ip).status_code == 200
    blocked = client.post("/api/research/run", headers=ip)
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "3600"


def test_daily_cap_applies_across_visitors(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    statuses = [
        client.post("/api/research/run", headers={"x-forwarded-for": f"203.0.113.{n}"}).status_code
        for n in range(5)
    ]
    assert statuses == [200, 200, 200, 429, 429]


def test_forged_forwarded_for_prefix_does_not_reset_the_budget(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/research/run", headers={"x-forwarded-for": "1.1.1.1, 203.0.113.9"}).status_code == 200
    forged = client.post("/api/research/run", headers={"x-forwarded-for": "2.2.2.2, 203.0.113.9"})
    assert forged.status_code == 429


def test_cached_briefing_is_unlimited_but_refresh_is_capped(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ip = {"x-forwarded-for": "203.0.113.8"}
    assert all(client.get("/api/dashboard/briefing", headers=ip).status_code == 200 for _ in range(10))
    refreshes = [client.get("/api/dashboard/briefing?refresh=true", headers=ip).status_code for _ in range(3)]
    assert refreshes == [200, 200, 429]


def test_writes_have_a_per_ip_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_limits, "WRITES_IP_PER_HOUR", 2)
    client = _client(tmp_path, monkeypatch)
    ip = {"x-forwarded-for": "203.0.113.10"}
    statuses = [client.put("/api/settings/models", headers=ip).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
