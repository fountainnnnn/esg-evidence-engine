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
    ip = {"x-real-ip": "203.0.113.7"}
    assert client.post("/api/research/run", headers=ip).status_code == 200
    blocked = client.post("/api/research/run", headers=ip)
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "3600"


def test_daily_cap_applies_across_visitors(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    statuses = [
        client.post("/api/research/run", headers={"x-real-ip": f"203.0.113.{n}"}).status_code
        for n in range(5)
    ]
    assert statuses == [200, 200, 200, 429, 429]


def test_forwarded_for_does_not_pick_the_bucket(tmp_path, monkeypatch):
    # Railway's rightmost X-Forwarded-For entry is its edge node, shared by many visitors.
    client = _client(tmp_path, monkeypatch)
    edge = "152.233.15.123"
    first = {"x-real-ip": "203.0.113.20", "x-forwarded-for": f"203.0.113.20, {edge}"}
    second = {"x-real-ip": "203.0.113.21", "x-forwarded-for": f"203.0.113.21, {edge}"}
    assert client.post("/api/research/run", headers=first).status_code == 200
    assert client.post("/api/research/run", headers=second).status_code == 200


def test_cached_briefing_is_unlimited_but_refresh_is_capped(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ip = {"x-real-ip": "203.0.113.8"}
    assert all(client.get("/api/dashboard/briefing", headers=ip).status_code == 200 for _ in range(10))
    refreshes = [client.get("/api/dashboard/briefing?refresh=true", headers=ip).status_code for _ in range(3)]
    assert refreshes == [200, 200, 429]


def test_other_true_spellings_of_refresh_are_capped_too(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ip = {"x-real-ip": "203.0.113.11"}
    statuses = [
        client.get(f"/api/dashboard/briefing?refresh={value}", headers=ip).status_code
        for value in ("TRUE", "yes", "On")
    ]
    assert statuses == [200, 200, 429]


def test_writes_have_a_per_ip_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_limits, "WRITES_IP_PER_HOUR", 2)
    client = _client(tmp_path, monkeypatch)
    ip = {"x-real-ip": "203.0.113.10"}
    statuses = [client.put("/api/settings/models", headers=ip).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
