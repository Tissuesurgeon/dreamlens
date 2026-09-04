# DreamLens

**Making DreamDEX Event Contracts easier to understand, trade, and automate.**

DreamLens is an intelligent trading layer built on top of DreamDEX Event Contracts on Somnia. It helps people discover live YES/NO markets, understand what they are buying and what they can lose, decide with AI-generated context instead of guesswork, trade in a few clicks, follow other traders under strict caps, and hand execution to a **DreamAgent** that can only act inside rules the user signs.

DreamDEX is the venue. DreamLens is the intelligent experience around it.

> Testnet only (Somnia Shannon). No real monetary value.

---

## Contents

- [What DreamLens is](#what-dreamlens-is)
- [The problem](#the-problem)
- [Discover → Understand → Decide → Trade → Learn](#discover--understand--decide--trade--learn)
- [AI that explains instead of predicting](#ai-that-explains-instead-of-predicting)
- [Smart Copy](#smart-copy)
- [DreamAgent](#dreamagent)
- [Trade check and Agent check](#trade-check-and-agent-check)
- [Decision Receipts](#decision-receipts)
- [Simple and Advanced](#simple-and-advanced)
- [Telegram](#telegram)
- [Why this matters](#why-this-matters)
- [Hackathon](#hackathon)
- [Architecture and authority model](#architecture-and-authority-model)
- [Stack](#stack)
- [Running locally](#running-locally)
- [Environment variables](#environment-variables)
- [Syncing live DreamDEX data](#syncing-live-dreamdex-data)
- [Shannon Delegation Framework](#shannon-delegation-framework)
- [Testing](#testing)
- [Further reading](#further-reading)

---

## What DreamLens is

DreamDEX provides Event Contracts: on-chain binary markets where a YES share and a NO share on the same question always add up to $1.00, and the winning side pays $1.00 at expiry. DreamLens does not replace that. It wraps it.

A user on DreamLens can:

- **Discover** live Event Contracts that people are already pricing
- **Understand** the question, the price, the payout, and the maximum loss in plain words
- **Decide** with AI context — why this market deserves attention, not a promise about the outcome
- **Trade** directly, copy a trader with caps, or let DreamAgent execute inside signed limits
- **Learn** from Decision Receipts and Somnia transactions for every fill

DreamLens is an application layer, not another exchange.

## The problem

Prediction markets are a useful way to express a view on a real-world outcome, but participation has friction. Before anything happens a person has to understand what the contract means, read the price, weigh the outcome, decide how much to risk, work a trading interface, and execute.

Take one question on the DreamDEX book:

> **Will Bitcoin be above $118,500 at expiry?** — YES $0.41 · NO $0.59

A new user immediately asks:

- What exactly am I buying?
- What does YES mean?
- What does $0.41 represent?
- How much can I lose? How much can I receive?
- When does the contract expire? Is it still active?
- What are other traders doing?
- Should I trade it at all?

These are not blockchain problems. They are decision-making and user-experience problems. When a user must understand all of this before they can participate, many simply leave. DreamLens was built to solve that on top of DreamDEX Event Contracts.

## Discover → Understand → Decide → Trade → Learn

The objective is simple: **users should know what they are trading before they trade it.**

| Step | What DreamLens shows |
| --- | --- |
| **Discover** | Live Event Contracts with the information that matters first: question, YES/NO price, time left, activity |
| **Understand** | The question in plain words; YES and NO in dollars; what $1, $5, $10 or $25 pays and what it can lose |
| **Decide** | A DreamLens Score plus a plain-language explanation of why the market is worth attention |
| **Trade** | Buy a side yourself, follow a trader under caps, or let DreamAgent act inside your rules — executed on DreamDEX, settled on Somnia |
| **Learn** | Every fill and agent decision comes back with a receipt you can read and a transaction you can look up |

The payout math is shown before funds are committed. $5 on YES at $0.41 buys 12.2 shares: maximum payout **$12.20**, potential profit **$7.20**, maximum loss **$5.00**.

## AI that explains instead of predicting

DreamLens does **not** tell users "YES has an 82% chance of winning." It explains why a market is worth paying attention to, using signals such as:

- Market activity
- Available liquidity
- Time remaining
- Recent price movement
- Trader activity (observed fills)
- Contract state (trading, closed, settled)

These combine into a **DreamLens Score** out of 100. The Score is an analysis signal that says "look here" — never a guaranteed probability, never a prediction of the outcome. Users can also ask questions in plain language ("Why is this trending?", "What happens if Bitcoin drops below the strike?") and get an explanation grounded in the live market.

**AI provides context. The user makes the decision.**

## Smart Copy

Not everyone wants to analyze markets themselves. Smart Copy lets users learn from other traders without blindly following a leaderboard. A trader profile shows what can actually be observed on-chain:

- Observed Event Contract fills and recency
- Consistency, trading frequency, sample size
- YES / NO split and preferred markets (BTC, ETH, other)

Copying is never unlimited. The user sets:

- **Maximum per trade** (for example $5)
- **Maximum daily allocation** (for example $20)
- **Minimum DreamLens Score** the copied market must meet (for example 75)

The result is controlled participation, not blind following. Copy-now requires an active DreamAgent; "Notify me" is a ping, not a fill. Users can unfollow a trader anywhere that trader appears.

## DreamAgent

DreamAgent moves DreamLens from assisted trading to autonomous execution. It monitors Event Contracts and executes eligible trades according to rules the user predefines, and it never has unrestricted access to the user's funds.

| DreamAgent **can** | DreamAgent **cannot** |
| --- | --- |
| Trade DreamDEX Event Contracts | Withdraw your funds |
| Copy selected traders | Change its own permissions |
| Execute trades within your limits | Exceed your trading limits |

The permission is scoped: maximum per trade, daily maximum, and an expiry (for example 30 days). In one sentence: *"You can trade for me, but only within these rules."*

## Trade check and Agent check

Automation raises an obvious question — *why did the system execute this trade?* — so DreamLens answers it before anything is sent.

**Trade check** (before a manual trade):

- Event is still active
- User has seen what the event means
- Amount is within the user's limit
- Maximum loss is shown
- Trading account is funded
- Executes through DreamDEX

**Agent check** (before DreamAgent acts):

- Followed trader is still eligible
- Event Contract is active
- Trade satisfies the user's limits
- DreamLens Score is above the user's threshold
- Daily allocation remaining
- Smart Account is funded

The agent can make decisions. The user's policies make the final rules. These checks are deterministic and live in the Policy and Risk engine; the AI cannot override them.

## Decision Receipts

Whenever DreamAgent executes — or intentionally skips — a trade, DreamLens records the reasoning and connects it to the position and the Somnia transaction. Not "the AI traded," but **"the AI traded because these conditions were satisfied."**

A receipt names the copied trader, the event, the side and price, each rule that passed (trader matched, event active, Score met the minimum, amount under the per-trade cap, daily limit remaining), the result, and the transaction hash. It is an auditable history of autonomous behavior.

## Simple and Advanced

DreamLens hides blockchain complexity, never trading consequences.

Users should not need contract addresses, transaction parameters, or order-book mechanics to participate. They should always be able to see what they are buying, how much they risk, the potential payout, the expiry, their position, the transaction, and what their agent did.

- **Simple mode** — the question, the prices, the risk, the decision
- **Advanced mode** — order books, liquidity, spreads, contract and transaction details, per-fill explorer links on trader and portfolio desks

## Telegram

The same desk is available on Telegram. Users can discover events, check prices, review outcomes, place trades, and monitor their agent from chat — same words, same risk information. Telegram is an interface, not a wallet: signing still happens in MetaMask, and the agent still runs inside the user's signed limits.

## Why this matters

DreamDEX provides the markets. Somnia provides the blockchain. DreamLens provides the intelligence, usability, and controlled automation that turn a complex market into a simple decision. Intelligence on top. On-chain execution underneath.

## Hackathon

Built for the **Somnia × DreamDEX Event Contracts Hackathon**. DreamLens syncs real Shannon testnet Event Contracts from the DreamDEX indexer and RPC (`MOCK_DREAMDEX=false` by default), places real `placeBinaryOrder` transactions through DreamDEX, and uses the MetaMask Delegation Framework deployed on Shannon for DreamAgent. There is no demo seed: an empty UI means `sync_dreamdex` has not run or the venue has no trading markets.

**An intelligent interface between people and on-chain prediction markets.**

---

## Architecture and authority model

```
User (MetaMask)
  → DreamLens (web + Telegram)
  → Policy & Risk Engine
  → Smart Account (MetaMask Hybrid DeleGator, owned by the user's MetaMask)
  → DreamDEX Event Contract
  → Somnia
```

- The user's MetaMask **owns** a Hybrid Smart Account. DreamLens never holds the user's private key.
- The user signs an **ERC-7710 delegation** that lets a backend session key redeem trades against DreamDEX only, capped by per-trade / daily limits and an expiry. Caveats are enforced on-chain by the Delegation Manager.
- The **agent cannot withdraw**. Only the owner can, via a one-shot owner-withdraw delegation signed in MetaMask (`POST /api/smart-account/withdraw/`, surfaced on the Portfolio and Agent pages).
- Manual trades still follow prepare → user signs → confirm.
- `MOCK_SMART_ACCOUNT` must stay `false` at runtime; the app refuses to create a mock account, grant, or broadcast when the live framework is not configured. Tests force it to `true` in `tests/conftest.py`.

Details: [`docs/architecture.md`](docs/architecture.md), [`docs/DREAMDEX_INTEGRATION.md`](docs/DREAMDEX_INTEGRATION.md), [`docs/SMART_ACCOUNT_INVESTIGATION.md`](docs/SMART_ACCOUNT_INVESTIGATION.md).

## Stack

- **Backend:** Python 3.12, Django 5, Django REST Framework, Celery, Redis
- **Database:** PostgreSQL via `DATABASE_URL` (Supabase session pooler in the reference deployment; any Postgres works). Tests use SQLite through `config.settings.test`.
- **Frontend:** Django templates, one hand-written CSS file (`static/css/dreamlens.css`, OKLCH design tokens), vanilla JS (`static/js/dreamlens.js`), Chart.js for the trader and portfolio desks. No CSS framework.
- **Chain:** DreamDEX Event Contracts on Somnia Shannon testnet (chain id `50312`); `web3.py` for reads, calldata and receipts; MetaMask Delegation Framework for DreamAgent
- **AI:** Cursor `composer-2.5` via `cursor-sdk` as primary; OpenRouter, Google, or a local OpenAI-compatible model (Ollama) as fallbacks
- **Integration code:** `integrations/dreamdex/` (GraphQL indexer + on-chain adapter), `integrations/metamask/` (delegation encoding, Smart Account deploy)

## Running locally

Prerequisites: Python 3.12+, a reachable Postgres (`DATABASE_URL`), Redis if you want Celery workers, and network access to the DreamDEX indexer and Somnia RPC.

```bash
git clone <this repo> dreamlens && cd dreamlens
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Required: DATABASE_URL (sslmode=require), SECRET_KEY
# Recommended: CURSOR_API_KEY for AI explanations (falls back to local LLM / rules otherwise)

python manage.py migrate
python manage.py sync_dreamdex          # pull live Shannon Event Contracts
python manage.py runserver --noreload   # http://127.0.0.1:8000/
```

`--noreload` avoids the dev autoreloader fighting with file watchers; it also means you must restart after template changes.

### Docker Compose

```bash
docker compose up --build
```

Services: `web` (gunicorn on 8000), `redis`, `worker` (Celery), `beat` (periodic `sync_dreamdex` and agent runs), `telegram` (long-polling bot for local dev), and an optional local `db` (Postgres 16). `web`, `worker`, `beat` and `telegram` read `.env`; Compose overrides `REDIS_URL` to the `redis` service.

### Telegram bot (optional)

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME`. Locally run `python manage.py telegram_poll`; in production point the BotFather webhook at the app and set `TELEGRAM_WEBHOOK_SECRET`.

## Environment variables

Mirrors [`.env.example`](.env.example).

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | — | Django secret key |
| `DEBUG` | `True` (local) | Debug mode; keep `False` in production |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,0.0.0.0` | Comma-separated hosts |
| `DJANGO_SETTINGS_MODULE` | `config.settings.local` | Local only; hosted platforms should use `config.settings.production` |
| `DATABASE_URL` | Postgres | ORM database. Supabase session pooler URL with `sslmode=require` in the reference deployment |
| `SUPABASE_URL` / `SUPABASE_KEY` | — | Optional Supabase Data API (not a Django DB backend) |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Celery broker / cache |
| `CSRF_TRUSTED_ORIGINS`, `CSRF_COOKIE_SECURE`, `SESSION_COOKIE_SECURE`, `SECURE_SSL_REDIRECT` | — / `False` | Production hardening |
| `LOG_LEVEL`, `DJANGO_LOG_LEVEL`, `DREAMLENS_LOG_LEVEL`, `CELERY_LOG_LEVEL` | `INFO` | Logging |
| `MOCK_DREAMDEX` | **`false`** | `true` = offline mock adapter (tests only) |
| `DREAMDEX_NETWORK` / `DREAMDEX_CHAIN_ID` | `testnet` / `50312` | Somnia Shannon |
| `DREAMDEX_RPC_URL` / `DREAMDEX_WS_RPC_URL` | Somnia infra RPC | On-chain reads, tx prep, receipts |
| `DREAMDEX_INDEXER_URL` | DreamDEX GraphQL | Markets, fills, trader activity |
| `DREAMDEX_VENUE_ID` | Shannon venue | Venue to sync; read `venueId` from a live Market row if markets come back empty |
| `DREAMDEX_EVENT_SYNC_INTERVAL` | `60` | Seconds between beat syncs |
| `DREAMDEX_COLLATERAL_DECIMALS`, `DREAMDEX_TICK`, `DREAMDEX_LOT` | `6`, `1000`, `1000` | Order encoding parameters |
| `DREAMDEX_BINARY_MODULE`, `DREAMDEX_COLLATERAL` | — | Optional contract overrides |
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_BASE_URL` | `cursor` / `composer-2.5` / Cursor API | Primary explanation model |
| `CURSOR_API_KEY` | — | Cursor user API key (`crsr_…`) |
| `LLM_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `LLM_REASONING` | — | Alternative providers |
| `LOCAL_LLM_ENABLED` / `LOCAL_LLM_BASE_URL` / `LOCAL_LLM_API_KEY` / `LOCAL_LLM_MODEL` | `true` / Ollama / `local` / `llama3.2` | OpenAI-compatible fallback |
| `MOCK_SMART_ACCOUNT` | **`false`** | `true` = pytest only; runtime refuses mock accounts and grants |
| `METAMASK_DELEGATION_MANAGER`, `METAMASK_SIMPLE_FACTORY`, `METAMASK_HYBRID_IMPL`, `METAMASK_ENTRY_POINT` | from deploy | Delegation Framework addresses on Shannon |
| `DREAM_AGENT_SESSION_KEY` | — | Backend session EOA that redeems delegations — **never** the user's MetaMask key |
| `DREAM_AGENT_GAS_LIMIT` | `800000` | Gas limit per redeem |
| `DREAM_AGENT_SA_PAYS_GAS`, `DREAM_AGENT_GAS_BUFFER_BPS`, `DREAM_AGENT_MAX_GAS_PAYMENT_WEI` | `true`, `2500`, `0.05 STT` | Smart Account reimburses session-key gas in STT, capped |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET` | — | Telegram bot; leave empty to disable |

Never put a user's MetaMask private key in `.env`.

## Syncing live DreamDEX data

```bash
python manage.py sync_dreamdex
```

1. Pulls binary markets for `DREAMDEX_VENUE_ID` from the DreamDEX GraphQL indexer and upserts `Event` rows by `external_id`
2. Indexes trader fills from the on-chain fill tape into `TraderProfile` / `TraderTrade`
3. Generates Event Radar signals (activity, liquidity, time left, price movement) that feed the DreamLens Score
4. Computes consensus snapshots from indexed trader activity

Celery beat repeats this every `DREAMDEX_EVENT_SYNC_INTERVAL` seconds (`workers.event_sync.full_event_sync_task`: events, prices, radar, then fill and copy processing). `python manage.py simulate_copy_alert` exercises the copy-alert path locally.

## Shannon Delegation Framework

DreamAgent needs the MetaMask Delegation Framework addresses on Shannon. Deploy or locate them once and merge the result into `.env`:

```bash
cd scripts/metamask
npm install
set -a && source ../../.env && set +a
SKIP_TRADE=1 MINIMAL_FRAMEWORK=1 node spike_deploy_and_trade.mjs
# merge scripts/metamask/env.fragment into .env (do not commit)
```

Until `METAMASK_*` and `DREAM_AGENT_SESSION_KEY` are set, "Activate DreamAgent" fails closed rather than falling back to a mock. See [`docs/SMART_ACCOUNT_INVESTIGATION.md`](docs/SMART_ACCOUNT_INVESTIGATION.md) for the delegation shape, caveats, and the owner-withdraw path.

## Testing

```bash
source .venv/bin/activate
pytest -q
```

Tests run with pytest-django on `config.settings.test` (SQLite, `MOCK_DREAMDEX=true`, `MOCK_SMART_ACCOUNT=true`). The suite covers:

- Risk engine and policy gates (expiry, limits, the AI cannot bypass them) — `test_risk_engine.py`, `test_live_fail_closed.py`
- Trade state machine and DreamDEX order encoding — `test_trading_state.py`, `test_dreamdex_order_encoding.py`
- Event sync upsert and market stats — `test_event_sync.py`, `test_market_stats.py`, `test_market_news.py`
- Copy relationships, dedup, suggested traders — `test_copy.py`, `test_suggested_traders.py`
- DreamAgent activation, gas reimbursement, Hybrid deploy, owner withdraw — `test_dream_agent.py`, `test_agent_gas.py`, `test_hybrid_deploy.py`, `test_owner_withdraw.py`
- AI structured output, intent parsing, Lens explanations — `test_ai_parse.py`, `test_lens.py`, `test_radar.py`
- Portfolio, wallet auth, Telegram bot, hosted runtime, Supabase client — `test_portfolio.py`, `test_wallet_auth.py`, `test_telegram_bot.py`, `test_hosted_runtime.py`, `test_supabase_client.py`
- Frontend copy and UX contracts (landing story, no `41¢`, no "chance of winning", no native popups, unfollow everywhere, setup info page) — `test_frontend_ux.py`

## Further reading

- [`docs/architecture.md`](docs/architecture.md) — component and sequence diagrams
- [`docs/DREAMDEX_INTEGRATION.md`](docs/DREAMDEX_INTEGRATION.md) — indexer queries, order encoding, receipts
- [`docs/SMART_ACCOUNT_INVESTIGATION.md`](docs/SMART_ACCOUNT_INVESTIGATION.md) — MetaMask Smart Accounts, ERC-7710 delegation, owner withdraw

## License

See project maintainers for license terms.
