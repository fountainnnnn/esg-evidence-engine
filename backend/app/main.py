"""Unified FastAPI layer.

Serves two feature sets from one server:
  * ESG research assistant — chat sessions, streaming agent, reports (app.agent / app.chat_history)
  * ESG Evidence Engine — precomputed scoring/signal/witness JSON + live news (backend.engine / backend.data)

Run from the repo root so the absolute ``backend.*`` engine imports resolve:
    uvicorn backend.app.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from backend.engine import config
from backend.engine.benchmarks import delete_benchmark, get_benchmarks, set_benchmark
from backend.engine.manual_raters import (
    delete_manual_rater,
    get_manual_raters,
    set_manual_rater,
)
from backend.engine.model_settings import get_model_settings, save_model_settings
from backend.engine.pipeline import build
from backend.engine.scrape_settings import get_scrape_settings, save_scrape_settings
from backend.engine.scraper_providers import check_selfhosted_endpoints
from backend.engine.source_intelligence import (
    delete_source_domain,
    get_company_intelligence,
    get_research_status,
    initialize_source_registry,
    list_source_registry,
    review_source_candidate,
    run_research,
    upsert_source_domain,
)

from .agent import (
    AssistantRequest,
    AssistantResponse,
    ChatMessage,
    OpenRouterAgent,
    ToolResult,
    collect_company_esg_news,
)
from .chat_history import (
    ChatHistoryStore,
    ChatSessionDetail,
    ChatSessionSummary,
    CreateChatSessionRequest,
)
from .usage_limits import CHAT_IP_PER_HOUR, allow_ip, client_ip, usage_limit_middleware
from .feedback import (
    FeedbackCreate,
    FeedbackRecord,
    FeedbackReview,
    FeedbackStore,
)
from .fact_overrides import (
    OVERRIDABLE_FIELDS,
    FactOverrideStore,
    OverrideCreate,
    OverrideRecord,
)


class Product(BaseModel):
    name: str
    value: int
    change: str


class Portfolio(BaseModel):
    customer_count: int
    transaction_volume: int
    risk_score: int
    uptime: str
    products: list[Product]


class ScrapeSettingsUpdate(BaseModel):
    providers: dict[str, bool] | None = None
    sourceTypes: dict[str, bool] | None = None
    frequency: str | None = None
    maxCompanies: int | None = None
    timezone: str | None = None
    runAt: str | None = None
    retainRawDays: int | None = None
    searxngBaseUrl: str | None = None
    crawl4aiBaseUrl: str | None = None


class ModelSettingsUpdate(BaseModel):
    provider: str | None = None
    openrouterModel: str | None = None
    bedrockModelId: str | None = None
    bedrockRegion: str | None = None
    temperature: float | None = None
    maxTokens: int | None = None


class ResearchRunRequest(BaseModel):
    companyId: str | None = None


class SourceReviewRequest(BaseModel):
    domain: str
    decision: str


class BenchmarkUpsertRequest(BaseModel):
    industry: str
    metric: str
    value: float
    source: str


class ManualRaterUpsertRequest(BaseModel):
    companyId: str
    rater: str
    valueRaw: str            # the rater's OWN scale — letter or number, validated server-side
    observedOn: str          # ISO date the value was read off the rater's page
    sourceUrl: str
    note: str | None = None
    assessmentYear: int | None = None   # the year the rating is FOR (defaults to END_YEAR)


class SourceUpsertRequest(BaseModel):
    domain: str
    sourceClass: str
    reason: str | None = None


app = FastAPI(title="PolyFintech 2026 API", version="1.0.0")
app.middleware("http")(usage_limit_middleware)
agent = OpenRouterAgent()
chat_history = ChatHistoryStore()
feedback_store = FeedbackStore()
override_store = FactOverrideStore()
_research_lock = threading.Lock()
_research_scheduler = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Agent rate limit — the site is public, so a global per-day cap on AI messages
# bounds API cost (the dashboard and all data stay unrestricted). Persisted to a
# gitignored file so the cap survives restarts.
# --------------------------------------------------------------------------- #
AGENT_DAILY_LIMIT = int(os.environ.get("AGENT_DAILY_LIMIT", "100"))
_RATE_FILE = Path(__file__).resolve().parents[2] / "cache" / "agent" / "agent_rate.json"


def _agent_rate_allow(request: Request) -> bool:
    """Count one agent message against this visitor's hourly budget and today's
    global cap. Returns False once either is spent, so the costly LLM call is skipped."""
    if not allow_ip("chat", client_ip(request), CHAT_IP_PER_HOUR):
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        data = json.loads(_RATE_FILE.read_text("utf-8")) if _RATE_FILE.exists() else {}
    except Exception:
        data = {}
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    if int(data.get("count", 0)) >= AGENT_DAILY_LIMIT:
        return False
    data["count"] = int(data.get("count", 0)) + 1
    try:
        _RATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _RATE_FILE.write_text(json.dumps(data), "utf-8")
    except Exception:
        pass
    return True


def _rate_limited_response() -> AssistantResponse:
    note = (
        f"The AI assistant limit has been reached ({CHAT_IP_PER_HOUR} messages per "
        f"visitor per hour, {AGENT_DAILY_LIMIT} per day across the site). The "
        "dashboard and all data are still fully available — please try the "
        "assistant again later."
    )
    return AssistantResponse(
        message=ChatMessage(role="assistant", content=note),
        tool_results=[
            ToolResult(name="rate_limit", status="error", summary=note, source_count=0)
        ],
        model=agent.model,
    )


# --------------------------------------------------------------------------- #
# ESG Evidence Engine — build precomputed JSON on startup, then serve it.
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def _ensure_built() -> None:
    # one-command run: seed the DB if empty, then precompute the dashboard JSON (offline).
    from backend.engine import ingest

    if not ingest.load().companies:
        from backend.data.seed import build as seed_build
        seed_build()
    if not (config.OUT_DIR / "companies.json").exists():
        build(offline=True)
    initialize_source_registry()
    _start_research_scheduler()


def _start_research_scheduler() -> None:
    """Schedule the global research pipeline from the persisted Settings values."""
    global _research_scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        _research_scheduler = BackgroundScheduler(daemon=True, timezone="Asia/Singapore")
        _research_scheduler.start()
        _reschedule_research()
    except Exception as exc:  # noqa: BLE001
        print(f"Research scheduler not started ({type(exc).__name__}).")


def _reschedule_research() -> None:
    if _research_scheduler is None:
        return
    from apscheduler.triggers.cron import CronTrigger

    settings = get_scrape_settings()
    hour, minute = (int(part) for part in settings["runAt"].split(":"))
    trigger_args: dict[str, object] = {
        "hour": hour,
        "minute": minute,
        "timezone": settings["timezone"],
    }
    if settings["frequency"] == "weekly":
        trigger_args["day_of_week"] = "mon"
    elif settings["frequency"] == "monthly":
        trigger_args["day"] = 1
    _research_scheduler.add_job(
        _launch_research,
        CronTrigger(**trigger_args),
        id="esg-research-refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


def _launch_research(company_id: str | None = None) -> bool:
    """Start research in a daemon thread and reject overlapping runs."""
    if not _research_lock.acquire(blocking=False):
        return False

    def worker() -> None:
        try:
            asyncio.run(run_research(agent.web_tools, company_id=company_id))
            # Keep the existing dashboard news snapshot fresh too.
            from backend.data.weekly import run_weekly
            run_weekly(force=True)
        except Exception as exc:  # noqa: BLE001
            print(f"Research refresh failed ({type(exc).__name__}: {str(exc)[:160]}).")
        finally:
            _research_lock.release()

    threading.Thread(target=worker, daemon=True, name="esg-research").start()
    return True


def _read(rel: str):
    path = config.OUT_DIR / rel
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{rel} not found — run the pipeline")
    return json.loads(path.read_text("utf-8"))


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "polyfintech-esg"}


@app.get("/api/settings/scraping")
def scraping_settings():
    return get_scrape_settings()


@app.put("/api/settings/scraping")
def update_scraping_settings(request: ScrapeSettingsUpdate):
    payload = request.model_dump(exclude_unset=True, exclude_none=True)
    try:
        result = save_scrape_settings(payload)
        _reschedule_research()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/settings/scraping/test")
async def test_scraping_endpoints():
    """Probe the self-hosted SearXNG/Crawl4AI base URLs so a misconfiguration shows
    up in Settings as a concrete error instead of silently empty results."""
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=25.0) as client:
        return await check_selfhosted_endpoints(client)


@app.get("/api/settings/models")
def model_settings():
    return get_model_settings()


@app.put("/api/settings/models")
def update_model_settings(request: ModelSettingsUpdate):
    payload = request.model_dump(exclude_unset=True, exclude_none=True)
    try:
        return save_model_settings(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/settings/benchmarks")
def benchmark_settings():
    return {"benchmarks": get_benchmarks()}


@app.put("/api/settings/benchmarks")
def update_benchmark(request: BenchmarkUpsertRequest):
    try:
        return {"benchmarks": set_benchmark(
            request.industry, request.metric, request.value, request.source)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/settings/benchmarks/{industry}/{metric}")
def remove_benchmark(industry: str, metric: str):
    try:
        return {"benchmarks": delete_benchmark(industry, metric)}
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"No stored benchmark for {industry}/{metric}.") from exc


@app.get("/api/settings/raters")
def rater_settings():
    return {"raters": get_manual_raters()}


@app.put("/api/settings/raters")
def update_manual_rater(request: ManualRaterUpsertRequest):
    try:
        return {"raters": set_manual_rater(
            request.companyId, request.rater, request.valueRaw,
            request.observedOn, request.sourceUrl, request.note,
            request.assessmentYear)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/settings/raters/{company_id}/{rater}")
def remove_manual_rater(company_id: str, rater: str):
    try:
        return {"raters": delete_manual_rater(company_id, rater)}
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"No hand-entered {rater} rating for {company_id}.") from exc


# --------------------------------------------------------------------------- #
# Interpretability — why the forecaster produced a given MSCI rating estimate.
# --------------------------------------------------------------------------- #
@app.get("/api/interpretability/model-card")
def interpretability_model_card():
    from backend.engine.interpret import model_card

    return model_card()


@app.get("/api/interpretability/predictions")
def interpretability_predictions():
    from backend.engine.interpret import explain_all

    return explain_all()


@app.get("/api/interpretability/company/{company_id}")
def interpretability_company(company_id: str):
    from backend.engine.interpret import explain

    try:
        return explain(company_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Company not found.") from exc


# --------------------------------------------------------------------------- #
# Governance — flagged agent responses, reviewed by a human, exported for RLHF.
# --------------------------------------------------------------------------- #
@app.post("/api/assistant/feedback", response_model=FeedbackRecord, status_code=201)
def create_feedback(request: FeedbackCreate) -> FeedbackRecord:
    return feedback_store.create(request)


@app.get("/api/assistant/feedback", response_model=list[FeedbackRecord])
def list_feedback(status: str | None = None, limit: int = 200) -> list[FeedbackRecord]:
    return feedback_store.list(status=status, limit=limit)


@app.get("/api/assistant/feedback/stats")
def feedback_stats():
    return feedback_store.stats()


@app.get("/api/assistant/feedback/export")
def export_feedback(all: bool = False) -> Response:
    body = feedback_store.export_jsonl(only_corrected=not all)
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": 'attachment; filename="rlhf-feedback.jsonl"',
        },
    )


@app.patch("/api/assistant/feedback/{feedback_id}", response_model=FeedbackRecord)
def review_feedback(feedback_id: str, request: FeedbackReview) -> FeedbackRecord:
    try:
        return feedback_store.review(feedback_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Feedback not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/assistant/feedback/{feedback_id}", status_code=204)
def delete_feedback(feedback_id: str) -> Response:
    if not feedback_store.delete(feedback_id):
        raise HTTPException(status_code=404, detail="Feedback not found.")
    return Response(status_code=204)


@app.get("/api/assistant/overrides/fields")
def override_fields():
    """The whitelist the Governance form renders. Kept server-side so the UI and
    the validator cannot drift apart."""
    return [
        {
            "field": field,
            "label": spec.get("label", field),
            "kind": spec["kind"],
            "min": spec.get("min"),
            "max": spec.get("max"),
            "hint": spec.get("hint", ""),
        }
        for field, spec in OVERRIDABLE_FIELDS.items()
    ]


@app.get("/api/assistant/overrides", response_model=list[OverrideRecord])
def list_overrides(
    company: str | None = None, include_expired: bool = True
) -> list[OverrideRecord]:
    return override_store.list(company_id=company, include_expired=include_expired)


@app.get("/api/assistant/overrides/stats")
def override_stats():
    return override_store.stats()


@app.post("/api/assistant/overrides", response_model=OverrideRecord, status_code=201)
def create_override(request: OverrideCreate) -> OverrideRecord:
    """Upsert by (companyId, field) — re-pinning a field replaces its value
    rather than stacking a second, ambiguous override."""
    try:
        return override_store.upsert(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/assistant/overrides/{override_id}", status_code=204)
def delete_override(override_id: str) -> Response:
    if not override_store.delete(override_id):
        raise HTTPException(status_code=404, detail="Override not found.")
    return Response(status_code=204)


@app.get("/api/research/status")
def research_status():
    return {**get_research_status(), "running": _research_lock.locked()}


@app.post("/api/research/run", status_code=202)
def start_research(request: ResearchRunRequest):
    if not _launch_research(request.companyId):
        raise HTTPException(status_code=409, detail="A research run is already active.")
    return {"status": "started", "scope": request.companyId or "universe"}


@app.get("/api/research/sources")
def research_sources():
    return list_source_registry()


@app.post("/api/research/sources")
def upsert_research_source(request: SourceUpsertRequest):
    try:
        return upsert_source_domain(request.domain, request.sourceClass, request.reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/research/sources/{domain:path}")
def delete_research_source(domain: str):
    try:
        return delete_source_domain(domain)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"{domain} is not in the registry.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/research/sources/review")
def review_research_source(request: SourceReviewRequest):
    try:
        return review_source_candidate(request.domain, request.decision)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Source candidate not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/research/company/{company_id}")
def company_research(company_id: str):
    try:
        return get_company_intelligence(company_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Company not found.") from exc


@app.get("/api/portfolio", response_model=Portfolio)
def portfolio() -> Portfolio:
    return Portfolio(
        customer_count=12840,
        transaction_volume=4280000,
        risk_score=18,
        uptime="99.98%",
        products=[
            Product(name="Digital Wallets", value=1820000, change="+18.4%"),
            Product(name="SME Lending", value=1410000, change="+11.2%"),
            Product(name="Cross-border Pay", value=1050000, change="+9.7%"),
        ],
    )


@app.get("/api/companies")
def companies():
    return _read("companies.json")


@app.get("/api/matrix")
def matrix():
    return _read("matrix.json")


@app.get("/api/signals")
def signals():
    return _read("signals.json")


@app.get("/api/regulations")
def regulations():
    """Catalog of tracked SG/ASEAN ESG regimes: who each binds + demo-set status counts."""
    return _read("regulations.json")


@app.get("/api/company/{company_id}")
def company(company_id: str):
    payload = _read(f"company/{company_id}.json")
    try:
        payload["liveIntelligence"] = get_company_intelligence(company_id)
    except KeyError:
        payload["liveIntelligence"] = None
    return payload


@app.get("/api/satellite/{company_id}")
def satellite(company_id: str, before: int = 2019, after: int = config.END_YEAR,
              limit: int = 6, asset_type: str | None = None, compute: bool = False,
              refresh: bool = False):
    """Before/after satellite observations of this company's registry-located assets.

    Reads what a batch run (`python -m backend.data.satverify`) already computed. Pass
    `compute=true` to fetch imagery inline — that costs four renders per site and can take
    minutes, so it is never the default for a page load.
    """
    from backend.engine.ingest import load
    from backend.engine.satellite import company_sites, observation_payload, observe_company

    try:
        comp = load().company(company_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown company {company_id}")

    types = [t.strip() for t in asset_type.split(",")] if asset_type else None
    read_only = not (compute or refresh)
    located = company_sites(comp.company_id, comp.name, limit, types, offline=read_only)
    observations = observe_company(comp.company_id, comp.name, before, after,
                                   limit=limit, asset_types=types, refresh=refresh,
                                   cached_only=not (compute or refresh))
    return {
        "company_id": comp.company_id,
        "company": comp.name,
        "before_year": before,
        "after_year": after,
        "located": len(located),
        "observations": [observation_payload(o) for o in observations],
        "disclosure": ("Coordinates come from OpenStreetMap; the dated before/after is "
                       "Sentinel-2 L2A via Element84 Earth Search. An NOT OBSERVED or "
                       "INCONCLUSIVE result means we could not observe construction — it is "
                       "not evidence a claim is false."),
    }


@app.get("/api/satellite/image/{name}")
def satellite_image(name: str):
    """Serve one rendered scene. Name is a flat filename — no traversal."""
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if "/" in name or "\\" in name or suffix not in ("png", "jpg"):
        raise HTTPException(status_code=400, detail="bad image name")
    path = (config.OUT_DIR / "satellite" / name).resolve()
    if not path.is_file() or path.parent != (config.OUT_DIR / "satellite").resolve():
        raise HTTPException(status_code=404, detail="image not found")
    media = "image/png" if suffix == "png" else "image/jpeg"
    # these are content-addressed by site id and never mutate in place
    return FileResponse(path, media_type=media,
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/news")
def news():
    """Live news/controversy scraped via Bright Data, served from the DB (durable)."""
    from backend.engine.db import bootstrap
    from backend.data.scrape import load_news

    conn = bootstrap()
    try:
        return load_news(conn)
    finally:
        conn.close()


@app.get("/api/dashboard/briefing")
def dashboard_briefing(refresh: bool = False):
    """Monday-morning manager digest: a desk-level overview plus one
    LLM-synthesized summary per covered company, cached per Asia/Singapore
    calendar day. ``refresh=true`` forces regeneration."""
    from backend.engine.briefing import get_or_generate_briefings

    return get_or_generate_briefings(refresh=refresh)


@app.get("/api/esg-news/company")
async def company_esg_news(
    company: str,
    ticker: str | None = None,
    domain: str | None = None,
    max_results: int = 8,
):
    """Live company-specific ESG/news evidence for any company in the UI universe."""
    result = await collect_company_esg_news(
        web_tools=agent.web_tools,
        company=company,
        ticker=ticker,
        domain=domain,
        max_results=max_results,
    )
    return {
        "company": company,
        "ticker": ticker,
        "domain": domain,
        "queries": result["queries"],
        "errors": result["errors"],
        "sources": [
            source.model_dump(by_alias=True) for source in result["sources"]
        ],
        "referenceArticles": [
            article.model_dump(by_alias=True)
            for article in result["reference_articles"]
        ],
    }


# --------------------------------------------------------------------------- #
# ESG research assistant — chat sessions + streaming agent.
# --------------------------------------------------------------------------- #
@app.get("/api/assistant/sessions", response_model=list[ChatSessionSummary])
def assistant_sessions() -> list[ChatSessionSummary]:
    return chat_history.list_sessions()


@app.post("/api/assistant/sessions", response_model=ChatSessionSummary)
def create_assistant_session(
    request: CreateChatSessionRequest,
) -> ChatSessionSummary:
    return chat_history.create_session(title=request.title)


@app.get("/api/assistant/sessions/{session_id}", response_model=ChatSessionDetail)
def assistant_session(session_id: str) -> ChatSessionDetail:
    try:
        return chat_history.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found.") from exc


@app.delete("/api/assistant/sessions/{session_id}", status_code=204)
def delete_assistant_session(session_id: str) -> Response:
    if not chat_history.delete_session(session_id):
        raise HTTPException(status_code=404, detail="Chat session not found.")
    return Response(status_code=204)


@app.post("/api/assistant/chat", response_model=AssistantResponse)
async def assistant_chat(request: AssistantRequest, http_request: Request) -> AssistantResponse:
    if not _agent_rate_allow(http_request):
        return _rate_limited_response()
    session = chat_history.ensure_session(request.session_id)
    effective_request = request.model_copy(update={"session_id": session.id})
    persist_latest_user_message(session.id, request)
    response = await agent.run(effective_request)
    chat_history.append_assistant_response(
        session.id,
        response,
        request.page_context,
    )
    return response


@app.post("/api/assistant/chat/stream")
async def assistant_chat_stream(request: AssistantRequest, http_request: Request) -> StreamingResponse:
    if not _agent_rate_allow(http_request):
        async def limited():
            yield json.dumps(
                {"type": "final", "response": _rate_limited_response().model_dump(by_alias=True)},
                ensure_ascii=False,
            ) + "\n"

        return StreamingResponse(limited(), media_type="application/x-ndjson")
    session = chat_history.ensure_session(request.session_id)
    effective_request = request.model_copy(update={"session_id": session.id})
    persist_latest_user_message(session.id, request)

    async def events():
        # Capture the detailed live trail (every search query + each URL opened)
        # so the finished message preserves what was scraped — not just the coarse
        # one-step-per-tool summary. Keep action starts + failures; drop the
        # redundant "ok" confirmations.
        live_steps: list[dict] = []
        async for event in agent.stream(effective_request):
            if event.get("type") == "workflow" and event.get("step"):
                step = event["step"]
                if step.get("status") in ("running", "error"):
                    live_steps.append(step)
            if event.get("type") == "final" and event.get("response"):
                resp_data = event["response"]
                if live_steps:
                    resp_data["workflowSteps"] = live_steps
                response = AssistantResponse(**resp_data)
                chat_history.append_assistant_response(
                    session.id,
                    response,
                    request.page_context,
                )
                event["response"] = response.model_dump(by_alias=True)
                event["session"] = chat_history.get_session_summary(
                    session.id,
                ).model_dump(by_alias=True)
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


def persist_latest_user_message(session_id: str, request: AssistantRequest) -> None:
    for message in reversed(request.messages):
        if message.role == "user":
            chat_history.append_user_message(
                session_id,
                message.content,
                request.page_context,
            )
            return
