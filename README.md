# PolyFintech 2026

React + FastAPI ESG evidence and agent workspace for Singapore-listed companies.

> Built by team WSP Squad for the PolyFintech 2026 competition: [@fountainnnnn](https://github.com/fountainnnnn), [@chodan1219](https://github.com/chodan1219) and [@zhixunnnnn](https://github.com/zhixunnnnn).

## Structure

- `frontend/` - Vite, React, TypeScript, Tailwind CSS
- `backend/` - FastAPI app with typed API responses

## Run Locally

Install frontend dependencies:

```bash
cd frontend
npm install
npm run dev
```

Install backend dependencies and run from the **repo root** (the engine uses
absolute `backend.*` imports):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.app.main:app --reload --port 8000
```

On first start the ESG Evidence Engine seeds its database and precomputes the
dashboard JSON offline (no network required); subsequent starts reuse it.

Open the frontend at `http://localhost:5173`.

The frontend proxies `/api/*` requests to `http://localhost:8000`.

## Usage limits

The deployment is public, so the endpoints that spend money are budgeted
(`backend/app/usage_limits.py`). The dashboard and precomputed data are not.

| Action | Per visitor (IP) | Whole site |
|---|---|---|
| AI assistant message | 20 per hour | 100 per day |
| Research run | 1 per hour | 3 per day |
| Live company news (`/api/esg-news/company`) | 20 per hour | 200 per day |
| Briefing regeneration (`?refresh=true`) | 2 per hour | 3 per day |
| Scraper endpoint test | 5 per hour | none |
| Any other write (`POST`/`PUT`/`PATCH`/`DELETE`) | 120 per hour | none |

Over a limit the API returns `429` with `Retry-After`; the assistant instead
replies with a message saying the limit was reached. Each limit can be changed
with an environment variable (`AGENT_DAILY_LIMIT`, `AGENT_IP_PER_HOUR`,
`RESEARCH_IP_PER_HOUR`, `RESEARCH_DAILY_LIMIT`, `NEWS_IP_PER_HOUR`,
`NEWS_DAILY_LIMIT`, `BRIEFING_REFRESH_IP_PER_HOUR`,
`BRIEFING_REFRESH_DAILY_LIMIT`, `SCRAPE_TEST_IP_PER_HOUR`,
`WRITES_IP_PER_HOUR`).

## Features

- **AI Assistant** — chat sessions, streaming research agent, source-backed PDF reports (`/api/assistant/*`).
- **ESG Evidence Engine** — scoring/signal/witness/forecast over a Singapore universe, served as `/api/companies`, `/api/matrix`, `/api/signals`, `/api/company/{id}`, and live `/api/news`. UI lives under the "Evidence Engine" and "Live News" sidebar sections.
- **Satellite verification** — check a physical claim ("we built a solar farm") against dated imagery, served as `/api/satellite/{id}`. See below.

## Satellite verification

Answers one narrow question well: *did the physical asset the company describes actually
get built?* It covers claims about things large enough to see from orbit — solar and wind
farms, plants, land clearing. It says nothing about emissions figures, board composition or
supplier audits, which is most of an ESG report.

No API keys. Every service is open:

| Step | Source |
| --- | --- |
| Asset coordinates + outline | OpenStreetMap via Overpass (`power=plant`, matched on `operator`/`name`) |
| Dated imagery | Sentinel-2 L2A via Element84 Earth Search (STAC over AWS open data) |
| Rendering a bbox to PNG | TiTiler |

```bash
python -m backend.engine.geolocate --company U96      # what can we locate?
python -m backend.engine.satellite --company U96 --type solar   # observe, print verdicts
python -m backend.data.satverify   U96                # observe AND apply to the score
```

Imagery is **precomputed in batch, never on page load** — a cold site costs four renders.
`/api/satellite/{id}` serves what `satverify` already wrote and never touches the network;
pass `compute=true` to fetch inline.

Detection is a **difference-in-differences** on a spectral index:

```
DiD = (index_after - index_before) on the asset footprint
    - (index_after - index_before) in the surrounding control area
```

The control leg cancels season, sun angle, haze and regional drift. Two indices are tried
and the stronger wins — NDWI (water cover) catches building over water, NDVI (vegetation
cover) catches building over land. A verdict of CHANGED additionally requires the asset
itself to have moved, so a big DiD caused purely by the *neighbours* building does not get
credited to this site.

Worked example — Sembcorp's Tengeh Floating Solar Farm, 2019 vs 2023: on-site NDWI falls
from +0.48 (open water) to +0.09 (panels) while the surrounding area drifts only −0.05.
DiD −0.33 → CHANGED.

### Effect on the ESG score

`satverify.py` is the satellite sibling of `verifyclaims.py`: imagery is simply another
independent corroborator, writing the same `state` / `corroboration_url` /
`corroboration_source` fields into the claims cache. A matched claim therefore moves
CREDIT_ASSERTED (0.5) → CREDIT_VERIFIED (1.0), its topic cap lifts ASSERTED_TOPIC_CAP (0.7)
→ 1.0, and its confidence 0.4 → 0.9. No change to `score.py`; the T3 invariant is untouched.

**The gate is the whole design.** An observation verifies a SITE; a claim is prose. Seeing
one farm get built says nothing about a portfolio total, so a claim is corroborated only
when all four hold:

1. the observation is OBSERVED (never NOT OBSERVED or INCONCLUSIVE);
2. the asset class may speak to that SASB topic — a gas plant never corroborates an
   energy-transition claim;
3. the claim asserts construction or operation, not intent — "committed to advancing the
   solar industry" is not "we built a solar farm";
4. the claim is not a portfolio aggregate ("3.8 GW across the group").

An earlier, looser gate upgraded a staff-training claim on the strength of a battery site,
because "Sembcorp" appears both in the site name and in nearly every claim the company
makes. The company's own name is now stripped from site-name matching, and every one of
those false positives is pinned by a test in `backend/tests/test_satverify.py`.

### In the app

- **Company page** — a "Ground truth" panel per located asset: a high-resolution view with
  the registry footprint outlined, a Sentinel-2 before/after with capture dates and cloud
  cover, the scene ids, and links out to OpenStreetMap, Google Maps and Google Earth so a
  reader can check independently.
- **Chat** — `get_satellite_verification` returns the same findings with image URLs the
  assistant can render inline.

Only **OBSERVED** is coloured. NOT OBSERVED and INCONCLUSIVE stay neutral: rendering them
in red would tell the reader the company lied, which is exactly what the engine refuses to
claim.

### What it will not do

- **Three states only: CHANGED / UNCHANGED / INCONCLUSIVE, and INCONCLUSIVE is the
  default.** No imagery, too much cloud, no registry match, or an ambiguous metric never
  becomes a verdict.
- **A negative is never "the company lied."** It is "we could not observe it." Pixels do
  not support the stronger claim, and publishing it about a named company would be
  defamation-adjacent.
- **Coordinates are never inferred by a model.** They come from a registry row with a
  citable permalink, or the site is skipped (guardrail T7).

Registered in `source_authority.json` as a **specialist** domain, so a positive observation
can raise a claim while a miss costs nothing — the same asymmetry `verify.py` already
applies to CDP and EcoVadis. Cloud cover is common enough in Southeast Asia that any other
rule would quietly penalise companies for the weather.

Known limits: OSM coverage is patchy outside Europe (Sembcorp's Indian wind is absent, so
the one claim that most wants checking cannot be); cloud is rejected per-scene rather than
per-pixel (the SCL band would fix this); and the matching gate is keyword-based, so it is
deliberately biased toward refusing rather than over-verifying.

## Scraper providers

All credentials are environment-only. Configure any combination and enable it from Settings:

```env
BRIGHTDATA_API_KEY=
BRIGHTDATA_API_KEY_FALLBACK=
SCRAPEDO_API_TOKEN=
OXYLABS_USERNAME=
OXYLABS_PASSWORD=
SEARXNG_BASE_URL=
CRAWL4AI_BASE_URL=
```

The pipeline fans out across enabled providers, canonicalizes URLs, groups duplicate claims, and stops after repeated discovery rounds yield no new evidence. It does not bypass login pages, authentication, or hard paywalls.

Start the free self-hosted stack locally with:

```bash
docker compose -f docker-compose.scrapers.yml up -d
```

Then set `SEARXNG_BASE_URL=http://localhost:8080` and `CRAWL4AI_BASE_URL=http://localhost:11235`.

For Railway, create two private services from this repository with root directories `infra/searxng` and `infra/crawl4ai`. Set `SEARXNG_SECRET` on the SearXNG service, then configure the main app with the Railway private-network URLs. These scraper services do not need public domains.

## Research controls

Settings provides Daily, Weekly (Monday), or Monthly (first day) scheduling at 06:00 Singapore time, provider and source-type selection, a full-universe Run now action, and manual review of source-promotion candidates. Raw extracted pages expire after 30 days; grouped claims and provenance remain in SQLite.

The deployed app is installable from `/app` using the browser's Add to Home Screen action.

## Data provenance (real vs. seeded)

This is a prototype, so the data is a deliberate mix of genuinely-sourced and
illustrative-seeded inputs. We keep this explicit rather than implying every
number is live.

**Real (genuinely sourced):**

- **Latest-year (2023) claims & evidence** — extracted by LLM directly from each
  company's actual sustainability-report PDF, then independently corroborated via
  web search (claims become `VERIFIED` only when a credible third-party source
  confirms them; otherwise `ASSERTED`; undisclosed material topics are filled by
  labelled `INFERRED` estimates).
- **Stock prices + STI benchmark** — real weekly OHLC scraped via Bright Data
  (Yahoo Finance, with native/MarketWatch fallbacks).
- **Live News** — real headlines scraped weekly via Bright Data SERP, then each
  headline is classified (controversy / positive / stock / neutral, or dropped as
  irrelevant) by an LLM, with a deterministic keyword classifier as the no-key
  fallback. The resulting sentiment is a real leading feature in the forecaster.
- **Company universe** (names, tickers, sectors, SASB industries) and the
  **regulation definitions** (SGX-711B, SGX Climate, IFRS S2/ISSB, MAS-ENRM,
  ASEAN Taxonomy) are real reference data.

**Seeded (illustrative — not live):**

- **Rater scores** (MSCI / S&P / Sustainalytics) and the derived consensus,
  divergence, and Trust Meter. These are commercial, paywalled products with no
  reliable free source, so they are hand-authored to illustrate rating spread.
- **Compliance statuses** (MET / PARTIAL / MISSING per regulation). The
  regulations themselves are real; each company's status is illustrative (no
  public ground-truth dataset exists).
- **Witness event pins** (CDP emissions-verified markers, controversy markers)
  on the 2019–2023 Price Witness chart, and the **pre-2023 evidence trajectory**.
  Real news is current-dated and cannot be placed on the fixed historical
  backtest window.
- **Fundamentals** (P/E, dividend yield) used as forecast inputs.

**Credential fallback.** Live extraction/verification/scraping require
`OPENROUTER_API_KEY` and Bright Data keys in a git-ignored `.env`. When those are
absent, every live path degrades gracefully to the seeded snapshot committed in
the repo (`backend/data/esg.db`, `backend/out/`, `backend/cache/`) — the app runs
fully offline with no network and no keys.

