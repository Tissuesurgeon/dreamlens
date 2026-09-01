# DreamLens

**See the event differently.**

DreamLens is an AI-native consumer interface on top of **DreamDEX Event Contracts** on Somnia. It helps people discover, understand, trade, and copy binary event markets — without replacing DreamDEX as the execution layer.

---

## Overview

DreamDEX provides on-chain Event Contracts (binary YES/NO markets). DreamLens wraps that infrastructure with a polished web experience, structured AI analysis, trader consensus, Event Radar, and DreamCopy (Smart Copy).

## Problem

Event Contracts are powerful but hard for mainstream users:

- Markets are spread across indexer APIs and on-chain contracts
- Raw prices and fills do not explain *why* a market matters
- Copying successful traders requires manual monitoring and discipline
- AI chatbots can suggest trades but should not bypass risk or sign transactions

## Solution

DreamLens adds an intelligence and UX layer:

| Lens | What it does |
| --- | --- |
| **Market** | Live prices, volume, expiry, order book context |
| **AI** | Structured DreamLens estimate (probability, signal, risks) |
| **Trader** | Indexed fills, consensus, top traders |
| **Copy** | Follow traders with SMART / CONSENSUS / BLIND modes |
| **Trade** | Prepare → sign → confirm flow with risk gates |
| **Dream Agent** | Optional autonomous Smart Copy via MetaMask Smart Account + delegated authority |

DreamDEX remains the source of truth for markets and execution. DreamLens never replaces the exchange.

**Authority model:** Your MetaMask wallet owns a DreamLens Smart Account. You grant DreamAgent limited `TRADE_EVENT_CONTRACT` permission (max per trade, daily cap, expiry). The agent cannot withdraw. See [`docs/SMART_ACCOUNT_INVESTIGATION.md`](docs/SMART_ACCOUNT_INVESTIGATION.md) and [`docs/architecture.md`](docs/architecture.md).

## What DreamLens adds on top of DreamDEX

- **Event Radar** — scored signals (consensus, momentum, volume, expiry)
- **Structured AI analysis** — JSON estimates with disclaimers, not open-ended guarantees
- **DreamCopy** — copy relationships with per-trade and daily limits
- **DreamAgent** — autonomous copy within delegated Smart Account limits
- **Risk Engine + Policy Engine** — deterministic checks AI cannot override
- **Unified UI** — browse, analyze, trade, copy, and manage the agent

---

## Architecture summary

```
User (MetaMask owner)
  → DreamLens Smart Account
  → DreamAgent (delegated session key)
  → Policy + Risk
  → DreamDEX Adapter → Event Contracts → Somnia

Manual trades still: prepare → user sign → confirm
```

Full diagrams: [`docs/architecture.md`](docs/architecture.md)

DreamDEX integration: [`docs/DREAMDEX_INTEGRATION.md`](docs/DREAMDEX_INTEGRATION.md)

Smart Account investigation: [`docs/SMART_ACCOUNT_INVESTIGATION.md`](docs/SMART_ACCOUNT_INVESTIGATION.md)

---

## Stack

- **Backend:** Python 3.12+, Django 5, DRF, Celery, Redis
- **Database:** PostgreSQL (Docker) or SQLite (local dev)
- **Frontend:** Django templates, Bootstrap 5, vanilla JS
- **Chain:** DreamDEX Event Contracts on Somnia (Shannon testnet for MVP)
- **Integration:** `integrations/dreamdex/` live GraphQL indexer + on-chain adapter (`MOCK_DREAMDEX=false` by default)

---

## Installation

### Prerequisites

- Python 3.12+
- Optional: Docker & Docker Compose (PostgreSQL + Redis + workers)

### Local (Neon Postgres)

```bash
cd /path/to/dreamlens
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Set DATABASE_URL to your Neon connection string (sslmode=require)

python manage.py migrate
python manage.py sync_dreamdex
python manage.py runserver --noreload
```

Open http://127.0.0.1:8000/

> Use `--noreload` if the dev autoreloader conflicts with your environment (e.g. certain file watchers or CI).

Offline SQLite is opt-in: `USE_SQLITE=1 python manage.py migrate`. Tests always use SQLite via `config.settings.test`.

Live data requires network access to `DREAMDEX_INDEXER_URL` and `DREAMDEX_RPC_URL`. Set `MOCK_DREAMDEX=true` only for offline unit tests.

### Docker Compose

```bash
docker compose up --build
```

Services: `web` (8000), `redis`, `worker`, `beat`. Postgres defaults to `DATABASE_URL` (Neon). The local `db` service is optional.

For Docker, set `USE_SQLITE=0` and your Neon `DATABASE_URL` in `.env`.

---

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `MOCK_DREAMDEX` | **`false`** | `true` = offline mock (tests only); `false` = live indexer/RPC |
| `USE_SQLITE` | **`0`** | `1` = local SQLite; default uses `DATABASE_URL` |
| `DATABASE_URL` | Neon Postgres | Default database (`sslmode=require`) |
| `SECRET_KEY` | (dev default) | Django secret key |
| `DEBUG` | `True` (local) | Debug mode |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker / cache |
| `DREAMDEX_CHAIN_ID` | `50312` | Somnia Shannon testnet |
| `DREAMDEX_VENUE_ID` | (see settings) | DreamDEX venue identifier |
| `DREAMDEX_INDEXER_URL` | GraphQL endpoint | Live adapter indexer |
| `DREAMDEX_RPC_URL` | Somnia RPC | On-chain reads / tx prep |
| `LLM_API_KEY` | (empty) | Optional; OpenRouter key can live in `OPENROUTER_API_KEY` instead |
| `OPENROUTER_API_KEY` | (empty) | Required for the primary Ling 3 Flash model |
| `GEMINI_API_KEY` | (empty) | Google AI Studio fallback when `LLM_PROVIDER=google` |
| `LLM_PROVIDER` | `openrouter` | `openrouter` (primary), `google`, or `ollama` |
| `LLM_MODEL` | `inclusionai/ling-3.0-flash-fin:free` | OpenRouter model id |
| `LLM_REASONING` | `true` | Sends `reasoning: {enabled: true}` to OpenRouter |
| `LOCAL_LLM_ENABLED` | `true` | Fall back to Ollama if OpenRouter fails |
| `LOCAL_LLM_BASE_URL` | `http://192.168.0.110:11434/v1` | OpenAI-compatible Ollama endpoint |
| `LOCAL_LLM_MODEL` | `llama3.2` | Local model id (`ollama pull llama3.2`) |
| `MOCK_SMART_ACCOUNT` | **`false`** | `true` = pytest only; runtime refuses mock SA / grant / broadcast |
| `METAMASK_DELEGATION_MANAGER` | (from deploy) | Delegation Manager on Shannon |
| `METAMASK_SIMPLE_FACTORY` | (from deploy) | SimpleFactory (`0x4766…b317` deployed) |
| `METAMASK_HYBRID_IMPL` | (from deploy) | Hybrid DeleGator `0xed13…dd92` |
| `METAMASK_ENTRY_POINT` | (from deploy) | EntryPoint used by Hybrid constructor |
| `DREAM_AGENT_SESSION_KEY` | (session EOA) | Backend key for `redeemDelegations` — never the user's MetaMask key |

---

## Syncing live DreamDEX data

```bash
python manage.py sync_dreamdex
```

This runs:

1. `sync_events` — pull binary markets from the DreamDEX GraphQL indexer
2. Index trader fills from on-chain fill tape
3. Generate Event Radar signals from live metrics
4. Compute consensus snapshots from indexed trader activity

There is **no demo seed**. Empty UI means sync has not run or the venue has no Trading markets.

### Delegation Framework (Shannon)

```bash
cd scripts/metamask
npm install
set -a && source ../../.env && set +a
SKIP_TRADE=1 MINIMAL_FRAMEWORK=1 node spike_deploy_and_trade.mjs
# merge scripts/metamask/env.fragment into .env (do not commit)
```

See [`docs/SMART_ACCOUNT_INVESTIGATION.md`](docs/SMART_ACCOUNT_INVESTIGATION.md).
---

## Testing

```bash
cd /path/to/dreamlens
source .venv/bin/activate
pytest -q
```

Tests use pytest-django with `config.settings.test` and SQLite.

Coverage includes:

- Risk engine (expiry, limits, AI cannot bypass)
- Trading state machine and fake external IDs
- Copy deduplication
- Radar scoring and signal generation
- AI structured output and intent parsing
- Event sync upsert and unique `external_id`

---

## Demo flow (10-step hackathon script)

1. **Start app** — `sync_dreamdex` + `runserver`; confirm home page loads with live events.
2. **Browse Event Radar** — highlight signals (consensus, volume, expiring soon).
3. **Open a BTC event** — Market lens shows YES/NO prices and volume.
4. **AI Lens** — run analysis; show estimate vs market probability and disclaimer.
5. **Trader Lens** — view indexed fills and consensus (YES/NO skew).
6. **Prepare trade** — enter amount, get unsigned `placeBinaryOrder` calldata; trade → AWAITING_CONFIRMATION.
7. **Confirm trade** — user wallet signs; trade → CONFIRMED after receipt.
8. **Follow a trader** — create CopyRelationship (SMART mode, limits).
9. **Trigger copy** — new TraderTrade → CopyExecution (risk + AI gate).
10. **Portfolio** — show positions, PnL summary, and copy history.

---

## Hackathon submission notes

- **DreamDEX integration:** Live GraphQL indexer + RPC adapter documented in [`docs/DREAMDEX_INTEGRATION.md`](docs/DREAMDEX_INTEGRATION.md). Mock adapter remains available for offline tests only (`MOCK_DREAMDEX=true`).
- **MOCK_DREAMDEX=false** is the default — the app syncs real Shannon testnet Event Contracts.
- **AI safety:** Estimates only; Risk Engine blocks expired events, bad outcomes, and low-confidence SMART copies.
- **No custodial keys:** Users sign transactions in their wallet; backend stores addresses only. The session EOA can only redeem a signed delegation.
- **MOCK_SMART_ACCOUNT=false** is the default — Activate Agent hard-errors until the Shannon Delegation Framework addresses are set.
- **Differentiation:** Lenses + Radar + DreamCopy on top of official DreamDEX Event Contracts — not a new exchange.

---

## Repository

DreamLens lives at `/home/richy/Desktop/dreamlens` as a sibling to the ADMIQ Consult site. ADMIQ is intentionally untouched.

---

## License

See project maintainers for license terms.
