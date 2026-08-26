# DreamDEX Event Contracts — Integration Research

**Project:** DreamLens  
**Phase:** 0 (research only)  
**Document date:** 2026-08-22  
**Status:** Findings from official sources. No invented APIs.

DreamLens is an intelligence + UX layer on top of DreamDEX Event Contracts.
DreamDEX remains the source of truth and execution layer.

---

## 1. Official sources inspected

| Source | URL / location | Role |
| --- | --- | --- |
| Event Contracts developer overview | https://docs.dreamdex.io/developers/event-contracts | Primary protocol surface |
| Recipes | https://docs.dreamdex.io/developers/event-contracts/recipes.md | Discover, book, order, mint, redeem |
| Market structure & lifecycle | https://docs.dreamdex.io/developers/event-contracts/market-structure.md | Contracts, statuses, escrow |
| Contracts & addresses | https://docs.dreamdex.io/developers/event-contracts/contracts-and-addresses.md | CREATE3 core addresses |
| Gotchas | https://docs.dreamdex.io/developers/event-contracts/gotchas.md | Indexer lag, float prices, venue scope |
| Trading product docs | https://docs.dreamdex.io/trading/event-contracts.md | Up/Down UX semantics |
| Settlement & voids | https://docs.dreamdex.io/trading/event-contracts/settlement-and-voids.md | Resolution / void payouts |
| Docs index | https://docs.dreamdex.io/llms.txt | Full doc map |
| HTTP API | https://docs.dreamdex.io/developers/http-api.md | **Spot only** — not Event Contracts |
| DreamDEX Bot Kit | https://github.com/somnia-chain/dreamdex-bot-kit | `docs/event-contracts.md`, `packages/ec-core` |
| `@somnia-chain/markets-sdk` | npm `0.28.1` README + `.d.ts` | Official TypeScript SDK |
| DreamBot Builder | https://dreambot-builder.vercel.app/ | Bot UI over the kit — **not** a DreamLens runtime |

---

## 2. Critical rule: Event Contracts ≠ DreamDEX REST

The DreamDEX HTTP API and public WebSocket feed are for **spot**:

| Environment | REST | Public WS |
| --- | --- | --- |
| Mainnet | `https://api.dreamdex.io/v0` | `wss://api.dreamdex.io/v0/ws/public` |
| Shannon testnet | `https://stg.api.dreamdex.io/v0` | `wss://stg.api.dreamdex.io/v0/ws/public` |

Official Event Contracts documentation states that the HTTP API **covers spot only and has no Event Contract endpoints**.

**Do not** use `GET /v0/markets` (or other `/v0/*` REST routes) for Event Contract discovery or trading.

The official Event Contract developer surface is:

1. **`@somnia-chain/markets-sdk`** (TypeScript) — `SomniaMarkets`
2. **GraphQL indexer** (Envio/Hasura)
3. **On-chain contracts** on Somnia (reads + signed writes)

For non-JS stacks, the docs explicitly say: pull ABIs from the SDK package (`binaryModuleReadAbi`, `binaryModuleWriteAbi`, `binarySettlementAbi`, `erc6909Abi`, `oracleHubAbi`, `binaryPoolWriteAbi`, …) and drive contracts with any RPC client.

---

## 3. What Event Contracts are

Binary **Up / Down** markets on crypto price windows (BTC and ETH today; 15-minute and 1-hour cadences).

- One on-chain CLOB; prices are **Up (YES) probabilities** in `(0, 1)`.
- Down (NO) price is always `1 − Up`.
- Settlement collateral: USDso on mainnet; faucet Test USDC on testnet.
- Winning contracts redeem **1** collateral unit each (dreamDEX settlement fee = 0).
- Voided markets: both sides redeem at **0.5**.
- Markets roll: windows expire and successors appear. Key state by **`marketId` or symbol**, never by pool address (pools are recycled).

### DreamLens naming map

| DreamLens | Protocol / SDK |
| --- | --- |
| YES | Up / YES (`…#YES` symbol) |
| NO | Down / NO |
| `EventContract.external_id` | `marketId` (bytes32 hex) |
| Outcome identifiers | `yesTokenId` / `noTokenId` (ERC-6909 ids) |

**Do not parse question text** for asset or window. Use typed fields: `asset`, `strike`, `intervalSec`, `expiry`, `tradingStart`.

---

## 4. Capability → official interface

### 4.1 Discover live Event Contracts

**SDK (documented):**

```ts
const markets = Object.values(await exchange.loadMarkets(true));
for (const m of markets) {
  if (!m.active || !isBinaryMarket(m.info)) continue;
  // …
}
```

**Client (documented):**

```ts
await exchange.client.listBinaryMarkets({
  venueId,
  status,      // e.g. live / Finalized depending on filter
  orderBy,     // "newest" | "closingSoon" | "volume" | "tradeCount"
  limit,
});
```

Always scope by **`venueId`** when present — one deployment hosts multiple venues; markets from all venues sit side by side in the indexer.

### 4.2 Retrieve Event Contract data

From a `BinaryMarket` row (SDK types, `markets.d.ts`):

| Field | Meaning |
| --- | --- |
| `marketId` / `id` | Stable identity (bytes32) |
| `marketAddress` | Per-window market contract |
| `poolAddress` | Current pool (time-varying binding) |
| `asset` | e.g. `"BTC"`, `"ETH"` |
| `strike` | Resolution strike (oracle scale) |
| `intervalSec` / `interval` | Window cadence |
| `tradingStart`, `expiry` | Window bounds |
| `yesTokenId`, `noTokenId` | ERC-6909 outcome ids |
| `collateral` | Collateral ERC-20 |
| `status` | Indexed lifecycle status |
| `venueId`, `operatorId` | Venue / operator scope |
| `oracleQuestionId` | Oracle explorer deep-link |
| `cumulativeQuoteVolume`, `cumulativeBaseVolume`, `tradeCount`, `lastPrice` | Activity |

### 4.3 Live status (gate every write)

Indexer **lags by seconds**. Before every trade:

```ts
const onchain = await exchange.client.getMarketOnchain(marketId);
// Only status 1 = Trading accepts orders
```

**Lifecycle (on-chain):**

| Status | Code | Notes |
| --- | --- | --- |
| Listed | 0 | Deployed, not open |
| Trading | 1 | Only state that accepts orders |
| Locked | 2 | Window ended; cancels still work |
| Settling | 3 | Enum exists; rarely observable |
| Resolved | 4 | Winner fixed; redeem winners |
| Voided | 5 | Both sides redeem at 0.5 |

### 4.4 Prices and order book

```ts
const [up, down] = market.outcomes ?? [];
const book = await exchange.fetchOrderBook(up.symbol, 5);
const bestBid = book.bids[0]?.[0];
const bestAsk = book.asks[0]?.[0];
```

Prices are Up probabilities in `(0, 1)`. Quoting the NO symbol converts to Up terms in the SDK.

**Volume:** use market-row cumulative volumes (divide by collateral decimals: **6 testnet**, **18 mainnet**). For ranking, `orderBy: "volume"` runs server-side.

**History:** `client.getCandles(pool, interval)` for Market Lens charts.

### 4.5 Positions

Outcome tokens are ids on one shared **ERC-6909** contract, not per-market ERC-20s:

```ts
const up = await exchange.client.getOutcomeBalance(onchain.outcomeToken, me, onchain.yesId);
const down = await exchange.client.getOutcomeBalance(onchain.outcomeToken, me, onchain.noId);
```

Also: `client.getPortfolio(account)` for indexed portfolio views.

### 4.6 Fills and trader activity

| Call | Use |
| --- | --- |
| `getFills(pool, opts)` | Public tape for a pool |
| `getUserFills(account, opts)` | Fills where wallet was maker or taker |
| `countUserFills(account, opts)` | Aggregate count |
| Live: `getLiveFills` / `watchUser` | Realtime after a watch |

**`FillRow` fields (documented):** `id`, `market` (stable marketId), `pool`, `fillPrice`, `quantity`, `quoteQuantity`, `maker`, `taker`, `makerSide`, `takerSide`, `kind`, `takerIsBid`, `takerOrder`, `timestamp`, `txHash`.

**There is no official trader leaderboard API.** DreamCopy / Trader Lens must index this fill tape (and settlement outcomes) ourselves.

Group by `market` (marketId), never by `pool` alone.

### 4.7 Construct and submit trades

**Unified (human units):**

```ts
await exchange.createOrder(yesSymbol, "limit", "buy", size, price, {
  timeInForce: "IOC",
});
```

Consumer MVP should prefer **IOC buys** so unfilled remainder does not rest with escrow locked.

**Raw trader tier (integer price/qty — required pattern on 18-decimal venues):**

```ts
await exchange.trader.placeOrder({
  pool: onchain.pool,
  side: "BUY_YES", // BUY_YES | SELL_YES | BUY_NO | SELL_NO
  price: ticks(0.05),
  quantity: lots(5),
  orderType: ORDER_TYPE.MARKET, // or LIMIT | FILL_OR_KILL | POST_ONLY
  expireTimestampNs: /* capped at market expiry */,
});
```

**Float gotcha:** `createOrder` uses `parseUnits(price.toFixed(18), 18)`. Many probabilities become off-tick on mainnet and revert with `InvalidPrice`. Snap to tick integers via the raw tier (or `ec-core` `placeLimit`).

**Lot sizing:** from markets-sdk ≥ 0.24.0 unified path reads pool lot size; raw path must quantize manually.

### 4.8 Wallet / signing flow

| Context | How signing works |
| --- | --- |
| Browser app | Pass a viem `walletClient` into the SDK trader; user confirms in wallet |
| Bot / server | Optional `privateKey` / local account (fixed fees, local nonce, `realtime_sendRawTransaction`) |

**DreamLens rule:** never store private keys. Backend prepares / validates trade intents and unsigned calldata from official ABIs; the user’s browser wallet signs and broadcasts.

Receipt location:

- Raw `trader.*` → receipt on the result
- Unified verbs → receipt on `order.info` as `PlaceOrderResult` (not `order.receipt`)

SDK ≥ 0.23.0: reverted writes throw decoded errors. Always check on-chain success.

### 4.9 Settlement and claims

Winnings are **claimed**, not auto-deposited.

1. Settled markets leave the live list; `loadMarkets()` does **not** return them for redeem-by-scan.
2. Query finalized binaries:

```ts
await exchange.client.listBinaryMarkets({
  venueId,
  status: "Finalized",
  limit: 120,
});
```

3. Redeem via `exchange.trader.redeem({ marketId, market, outcomeToken, outcomeIdx, amount })`.
4. Voided: claim both sides at 0.5. Resolved: only the winning side pays. Redeeming a loser succeeds and pays 0.

Oracle audit UI: `https://prd.oracle.somnia.host/questions/{oracleQuestionId}?view=graph`

### 4.10 Realtime / subscriptions

- Event Contract realtime in the official SDK: indexer snapshot + chain log WebSocket live watches (`watchMarket`, `watchMarkets`, `watchUser`).
- DreamDEX public WS (`wss://…/v0/ws/public`) is the **spot** feed — do not treat it as Event Contract market data.
- If DreamLens cannot run the TS live-tail loop, poll GraphQL + on-chain status with configurable `DREAMDEX_EVENT_SYNC_INTERVAL`.

### 4.11 Complete sets (inventory)

```ts
await exchange.mintSet(market.symbol, 10); // collateral → Up + Down
await exchange.burnSet(market.symbol, 10);
```

Not required for two-sided quoting via mint-a-pair when opposite buyers cross.

---

## 5. Networks and endpoints

Hackathon target: **Shannon testnet** (`50312`).

| | Shannon testnet | Mainnet |
| --- | --- | --- |
| Chain ID | `50312` | `5031` |
| HTTP RPC | `https://api.infra.testnet.somnia.network` (bot-kit also documents `https://dream-rpc.somnia.network`) | `https://api.infra.mainnet.somnia.network` |
| WS RPC | `wss://api.infra.testnet.somnia.network/ws` | `wss://api.infra.mainnet.somnia.network/ws` |
| Event indexer GraphQL | `https://dev.smk.somnia.host/v1/graphql` | `https://prd.smk.somnia.host/v1/graphql` |
| Collateral | Test USDC `0x70a86D8842FB63C4Ad2b7cdddF530eBf1BB25d8E` (6 decimals, faucet) | USDso `0x00000022dA000002656c64D9eA6011ea952D008A` (18 decimals) |

SDK constructor shape (from npm README):

```ts
new SomniaMarkets({
  indexerUrl,
  chain,       // somniaShannon | somniaMainnet
  wsRpcUrl,
  addresses,   // SOMNIA_TESTNET_ADDRESSES | SOMNIA_MAINNET_ADDRESSES
  privateKey,  // optional
});
```

---

## 6. Core contract addresses

Protocol core is CREATE3-deterministic and **identical on testnet and mainnet** (confirm on explorer before real funds):

| Contract | Address |
| --- | --- |
| BinaryMarketsModule | `0x3ecC694Cef705358864a646142ac17A90E29e388` |
| MarketsCore | `0x2802504314685D89bF6C992CA5a8e7cC78bc0294` |
| BinarySettlement | `0xbF4a49e0Dfd092e5FBE8E5761064C49533e6Ed23` |
| OutcomeToken6909 | `0xB52c5934113Af5c0Bb20eb3C72290C8215f755b9` |
| OracleHub | `0xe40db387cC98601Dd11bd634fF2f3AD5686dE32b` |
| CollateralRouter | `0xbC0C9834B15ACE38bB50dDaa7d7f7C7CC4DC183C` |

**Never hardcode** per-window market or pool addresses. Read from module registry / SDK / indexer.

Bot-kit `ec-core` also bundles `clobFactory`, `binaryPoolImpl`, `marketCreatorFactory`, and network-specific `marketCreator` / collateral. Prefer SDK address constants + env overrides when redeploys land.

Explorers: [mainnet](https://explorer.somnia.network), [testnet](https://shannon-explorer.somnia.network).

---

## 7. Venue ID (moves — env only)

Bot-kit starting points (already changed multiple times in early August 2026):

| Network | Starting `VENUE_ID` |
| --- | --- |
| Testnet | `0x679795a0195a1b76cdebb7c51d74e058aee92919b8c3389af86ef24535e8a28c` |
| Mainnet | `0x458b30c2d72bfd2c6317304a4594ecbafe5f729d3111b65fdc3a33bd48e5432d` |

If a bot/app reports no markets, or live markets span several venues, **read `venueId` off a live market row**. Do not treat the table above as permanent.

Tick / lot are **not** on binary market rows. Bot-kit defaults:

| Network | Tick | Lot |
| --- | --- | --- |
| Testnet | `1000` | `1` |
| Mainnet | `1e15` | `1e15` |

Expose as env (`DREAMDEX_TICK`, `DREAMDEX_LOT`).

---

## 8. DreamLens adapter strategy

All DreamDEX-specific logic lives under `integrations/dreamdex/`.

### Interface (sketch — implement in Phase 2)

```python
class DreamDEXAdapter(Protocol):
    def list_events(self, *, venue_id: str | None = None, status: str | None = None) -> list[EventDTO]: ...
    def get_event(self, market_id: str) -> EventDTO: ...
    def get_market_onchain(self, market_id: str) -> OnchainMarketDTO: ...
    def get_order_book(self, yes_symbol: str, depth: int = 5) -> OrderBookDTO: ...
    def get_candles(self, pool: str, interval: int) -> list[CandleDTO]: ...
    def get_fills(self, pool: str, *, since: int | None = None) -> list[FillDTO]: ...
    def get_user_fills(self, account: str, *, pool: str | None = None) -> list[FillDTO]: ...
    def get_outcome_balances(self, account: str, market_id: str) -> OutcomeBalancesDTO: ...
    def prepare_place_order(self, intent: TradeIntent) -> UnsignedTxDTO: ...
    def list_finalized_events(self, *, venue_id: str | None = None) -> list[EventDTO]: ...
    def prepare_redeem(self, ...) -> UnsignedTxDTO: ...
```

### Implementations

| Adapter | When | Behavior |
| --- | --- | --- |
| `MockDreamDEXAdapter` | `MOCK_DREAMDEX=true` | Realistic fake markets, prices, traders, fills, settlements |
| `DreamDEXAdapter` (live) | `MOCK_DREAMDEX=false` | GraphQL indexer + `web3` + official ABIs |

Optional later (only if GraphQL/ABI path hits a **documented** blocker): thin Node sidecar wrapping `@somnia-chain/markets-sdk`. Not required for Phase 0–1.

### Bot kit usefulness

- **`packages/ec-core`**: opinionated wrapper — venue filter, `placeLimit`, claim helpers, gotcha guards. Reference implementation for DreamLens trading service.
- **`strategies/ec-*`**: examples only (`ec-starter`, `ec-maker`, …).
- **`packages/core-py`**: spot bots — **not** Event Contracts.

---

## 9. Gaps and TODOs (do not invent)

| Gap | Handling |
| --- | --- |
| No official Python Event Contract SDK | Live adapter: documented GraphQL + exported ABIs via `web3` |
| GraphQL query documents not published as standalone docs | **TODO Phase 2:** install `@somnia-chain/markets-sdk`, extract typed documents / introspect Hasura at `https://dev.smk.somnia.host/v1/graphql`. Never invent field names. |
| No trader leaderboard | Index `getFills` / `getUserFills`; leftover seed `0xAlpha…` rows are purged |
| Spot REST/WS | Out of scope for Event Contracts |
| DreamBot Builder | Not a runtime dependency |
| Mainnet spot price feed for underlying BTC/ETH | Testnet has `SOMNIA_TESTNET_PRICE_FEED`; optional for AI Lens, not required to trade |
| Unsigned browser tx encoding details beyond ABIs | Phase 4: encode with `binaryPoolWriteAbi` / module write ABI; confirm against SDK `trader.placeOrder` params |

---

## 10. Planned DreamLens trade flow (aligned with official browser path)

1. Discover via indexer (`listBinaryMarkets` / equivalent GraphQL).
2. Re-validate on-chain (`getMarketOnchain`; status must be Trading).
3. Quote from book; snap price to tick and size to lot as integers.
4. Backend encodes unsigned place-order calldata from official ABI.
5. User confirms in Trade Lens; browser wallet signs and sends.
6. Backend records tx hash, waits for receipt, polls indexer for fill/position.
7. After expiry, sync Finalized markets and expose redeem (user-signed; not auto-execute by AI).

Transaction state machine (application-level):  
`PREPARED` → `AWAITING_CONFIRMATION` → `SUBMITTED` → `CONFIRMED` | `FAILED` | `EXPIRED`.

---

## 11. Environment variables (integration-related)

```bash
MOCK_DREAMDEX=false
DREAMDEX_NETWORK=testnet
DREAMDEX_CHAIN_ID=50312
DREAMDEX_RPC_URL=https://api.infra.testnet.somnia.network
DREAMDEX_WS_RPC_URL=wss://api.infra.testnet.somnia.network/ws
DREAMDEX_INDEXER_URL=https://dev.smk.somnia.host/v1/graphql
DREAMDEX_VENUE_ID=0x679795a0195a1b76cdebb7c51d74e058aee92919b8c3389af86ef24535e8a28c
DREAMDEX_EVENT_SYNC_INTERVAL=15
DREAMDEX_COLLATERAL_DECIMALS=6
DREAMDEX_TICK=1000
DREAMDEX_LOT=1000

# Optional overrides
DREAMDEX_BINARY_MODULE=
DREAMDEX_COLLATERAL=

# MetaMask Delegation Framework (deploy via scripts/metamask/spike_deploy_and_trade.mjs)
MOCK_SMART_ACCOUNT=false
METAMASK_DELEGATION_MANAGER=
METAMASK_SIMPLE_FACTORY=
METAMASK_HYBRID_IMPL=
METAMASK_ENTRY_POINT=
DREAM_AGENT_SESSION_KEY=

# Never store the user's MetaMask key
# PRIVATE_KEY=
```

`MOCK_DREAMDEX=true` and `MOCK_SMART_ACCOUNT=true` are for pytest only (`tests/conftest.py`).

---

## 12. Explicit “do not invent” list

Do **not** invent or assume:

- DreamDEX REST Event Contract endpoints
- Spot `/v0` methods as Event Contract APIs
- Unverified GraphQL field names before schema/SDK extraction
- Hardcoded market or pool addresses
- Permanent venue IDs without live verification
- A public trader leaderboard API
- Auto-settlement without a redeem call
- Python SDK methods that do not exist upstream
- AI-generated contract calls without ABI validation

Prefer: adapters, mocks, and clear TODOs over fabricated APIs.

---

## 13. Model field mapping (for Phase 1+)

| DreamLens model field | Source |
| --- | --- |
| `EventContract.external_id` | `marketId` |
| `title` / display | Prefer `asset` + `interval` + strike/window; question text display-only |
| `underlying_asset` | `asset` |
| `expiry_time` | `expiry` |
| `yes_identifier` / `no_identifier` | `yesTokenId` / `noTokenId` or outcome symbols |
| `EventOutcome.current_price` | Book mid / lastPrice (YES); NO = 1 − YES |
| `EventSnapshot` | Periodic book + volume snapshots |
| `Trade.transaction_hash` | Fill / place-order receipt |
| `TraderProfile.wallet_address` | Fill `maker` / `taker` |
| `TraderTrade` | Indexed fills + settlement PnL |

DreamLens DB is an indexed cache. DreamDEX / chain remains authoritative.

---

## 14. Phase gate

This document completes **Phase 0**.

**Next (requires approval):** Phase 1 — Django scaffolding, PostgreSQL, Redis, Celery, Docker, base layout. No live trading code until Phase 2 after GraphQL/ABI extraction is verified against official sources.
