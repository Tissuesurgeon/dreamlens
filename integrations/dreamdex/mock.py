"""Mock DreamDEX adapter with realistic demo seed data."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.utils import timezone as django_tz

from integrations.dreamdex.exceptions import DreamDEXNotFound, DreamDEXValidationError
from integrations.dreamdex.types import (
    CandleDTO,
    EventDTO,
    FillDTO,
    OnchainMarketDTO,
    OrderBookDTO,
    OrderBookLevel,
    OutcomeBalancesDTO,
    TradeIntent,
    UnsignedTxDTO,
    WalletBalancesDTO,
)

TWO_PLACES = Decimal("0.01")
VENUE_ID = "0x679795a0195a1b76cdebb7c51d74e058aee92919b8c3389af86ef24535e8a28c"
COLLATERAL = "0x70a86D8842FB63C4Ad2b7cdddF530eBf1BB25d8E"
OUTCOME_TOKEN = "0xB52c5934113Af5c0Bb20eb3C72290C8215f755b9"
BINARY_POOL = "0x2802504314685D89bF6C992CA5a8e7cC78bc0294"

TRADER_WALLETS = {
    "AlphaTrader": "0xAlpha000000000000000000000000000000000001",
    "Nova": "0xNova00000000000000000000000000000000000002",
    "Orbit": "0xOrbit0000000000000000000000000000000000003",
}


def _market_id(seed: str) -> str:
    digest = hashlib.sha256(seed.encode()).hexdigest()
    return f"0x{digest}"


def _token_id(market_id: str, side: str) -> str:
    digest = hashlib.sha256(f"{market_id}:{side}".encode()).hexdigest()[:16]
    return str(int(digest, 16))


def _quantize_price(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _split_prices(yes_price: Decimal) -> tuple[Decimal, Decimal]:
    yes = _quantize_price(yes_price)
    no = _quantize_price(Decimal("1") - yes)
    return yes, no


class MockDreamDEXAdapter:
    """In-memory DreamDEX adapter for local UI and hackathon demos."""

    def __init__(self) -> None:
        now = django_tz.now()
        self._venue_id = settings.DREAMDEX_VENUE_ID or VENUE_ID
        self._chain_id = settings.DREAMDEX_CHAIN_ID
        self._events: dict[str, EventDTO] = {}
        self._finalized: dict[str, EventDTO] = {}
        self._fills: dict[str, list[FillDTO]] = {}
        self._price_history: dict[str, list[Decimal]] = {}
        self._redeemed: dict[tuple[str, str], dict[str, Decimal]] = {}
        self._seed_events(now)
        self._seed_fills(now)
        self._seed_finalized(now)

    def _make_event(
        self,
        *,
        seed: str,
        asset: str,
        interval_sec: int,
        yes_price: Decimal,
        minutes_to_expiry: int,
        volume: Decimal,
        trade_count: int,
        now: datetime,
        status: str = "live",
    ) -> EventDTO:
        market_id = _market_id(seed)
        yes, no = _split_prices(yes_price)
        expiry = now + timedelta(minutes=minutes_to_expiry)
        trading_start = expiry - timedelta(seconds=interval_sec)
        yes_token = _token_id(market_id, "YES")
        no_token = _token_id(market_id, "NO")
        yes_symbol = f"{asset}-{interval_sec // 60}m-{market_id[-6:]}#YES"
        no_symbol = f"{asset}-{interval_sec // 60}m-{market_id[-6:]}#NO"
        opening = Decimal("98500.00") if asset == "BTC" else Decimal("3450.00")
        strike = int(opening * 100)

        return EventDTO(
            market_id=market_id,
            asset=asset,
            strike=strike,
            interval_sec=interval_sec,
            expiry=expiry,
            trading_start=trading_start,
            yes_token_id=yes_token,
            no_token_id=no_token,
            yes_price=yes,
            no_price=no,
            cumulative_quote_volume=volume,
            last_price=yes,
            venue_id=self._venue_id,
            status=status,
            pool_address=f"0xpool{market_id[2:10]}",
            market_address=f"0xmarket{market_id[2:10]}",
            collateral=COLLATERAL,
            trade_count=trade_count,
            oracle_question_id=f"oracle-{market_id[2:10]}",
            cumulative_base_volume=volume * Decimal("0.8"),
            yes_symbol=yes_symbol,
            no_symbol=no_symbol,
            opening_price=opening,
        )

    def _seed_events(self, now: datetime) -> None:
        specs = [
            ("btc-15m-demo", "BTC", 900, Decimal("0.43"), 12, Decimal("84200"), 156),
            ("btc-15m-2", "BTC", 900, Decimal("0.52"), 8, Decimal("42100"), 89),
            ("btc-1h-1", "BTC", 3600, Decimal("0.61"), 34, Decimal("128500"), 210),
            ("eth-15m-1", "ETH", 900, Decimal("0.38"), 10, Decimal("31500"), 72),
            ("eth-15m-2", "ETH", 900, Decimal("0.55"), 6, Decimal("28700"), 64),
            ("eth-1h-1", "ETH", 3600, Decimal("0.47"), 28, Decimal("95600"), 133),
            ("btc-15m-3", "BTC", 900, Decimal("0.49"), 14, Decimal("19800"), 41),
            ("eth-1h-2", "ETH", 3600, Decimal("0.58"), 45, Decimal("67200"), 98),
        ]
        for seed, asset, interval, yes_price, mins, volume, trades in specs:
            event = self._make_event(
                seed=seed,
                asset=asset,
                interval_sec=interval,
                yes_price=yes_price,
                minutes_to_expiry=mins,
                volume=volume,
                trade_count=trades,
                now=now,
            )
            self._events[event.market_id] = event
            self._price_history[event.market_id] = [yes_price - Decimal("0.03"), yes_price]

    def _seed_fills(self, now: datetime) -> None:
        btc_demo = next(
            e for e in self._events.values() if e.asset == "BTC" and e.interval_sec == 900 and e.yes_price == Decimal("0.43")
        )
        pool = btc_demo.pool_address or ""
        self._fills[pool] = []

        fill_specs = [
            ("AlphaTrader", "YES", Decimal("0.42"), Decimal("25"), timedelta(minutes=8)),
            ("Nova", "YES", Decimal("0.43"), Decimal("18"), timedelta(minutes=6)),
            ("Orbit", "YES", Decimal("0.44"), Decimal("12"), timedelta(minutes=4)),
            ("AlphaTrader", "YES", Decimal("0.43"), Decimal("30"), timedelta(minutes=2)),
            ("Nova", "NO", Decimal("0.57"), Decimal("10"), timedelta(minutes=1)),
        ]
        for idx, (trader, side, price, qty, ago) in enumerate(fill_specs, start=1):
            ts = now - ago
            maker = TRADER_WALLETS[trader]
            taker = TRADER_WALLETS["Orbit" if trader != "Orbit" else "Nova"]
            quote_qty = price * qty
            fill = FillDTO(
                id=f"fill-{btc_demo.market_id[-8:]}-{idx}",
                market_id=btc_demo.market_id,
                pool=pool,
                fill_price=price,
                quantity=qty,
                quote_quantity=quote_qty,
                maker=maker,
                taker=taker,
                maker_side=side,
                taker_side="NO" if side == "YES" else "YES",
                kind="LIMIT",
                taker_is_bid=side == "YES",
                taker_order=f"order-{idx}",
                timestamp=ts,
                tx_hash=f"0x{secrets.token_hex(32)}",
                trader_label=trader,
            )
            self._fills[pool].append(fill)

        for event in self._events.values():
            if event.market_id == btc_demo.market_id:
                continue
            pool = event.pool_address or ""
            self._fills[pool] = [
                FillDTO(
                    id=f"fill-{event.market_id[-8:]}-1",
                    market_id=event.market_id,
                    pool=pool,
                    fill_price=event.yes_price,
                    quantity=Decimal("5"),
                    quote_quantity=event.yes_price * Decimal("5"),
                    maker=TRADER_WALLETS["AlphaTrader"],
                    taker=TRADER_WALLETS["Nova"],
                    maker_side="YES",
                    taker_side="NO",
                    kind="LIMIT",
                    taker_is_bid=True,
                    taker_order="order-1",
                    timestamp=now - timedelta(minutes=3),
                    tx_hash=f"0x{secrets.token_hex(32)}",
                    trader_label="AlphaTrader",
                )
            ]

    def _seed_finalized(self, now: datetime) -> None:
        settled = self._make_event(
            seed="btc-15m-settled",
            asset="BTC",
            interval_sec=900,
            yes_price=Decimal("0.72"),
            minutes_to_expiry=-15,
            volume=Decimal("120400"),
            trade_count=188,
            now=now,
            status="Finalized",
        )
        settled = replace(
            settled,
            winning_outcome="YES",
            status="Finalized",
        )
        self._finalized[settled.market_id] = settled

        voided = self._make_event(
            seed="eth-15m-voided",
            asset="ETH",
            interval_sec=900,
            yes_price=Decimal("0.50"),
            minutes_to_expiry=-30,
            volume=Decimal("45200"),
            trade_count=95,
            now=now,
            status="Voided",
        )
        voided = replace(
            voided,
            winning_outcome=None,
            status="Voided",
        )
        self._finalized[voided.market_id] = voided

    def list_events(
        self,
        *,
        venue_id: str | None = None,
        status: str | None = None,
    ) -> list[EventDTO]:
        events = list(self._events.values())
        if venue_id:
            events = [e for e in events if e.venue_id == venue_id]
        if status:
            events = [e for e in events if e.status.lower() == status.lower()]
        return sorted(events, key=lambda e: e.expiry)

    def get_event(self, market_id: str) -> EventDTO:
        event = self._events.get(market_id) or self._finalized.get(market_id)
        if not event:
            raise DreamDEXNotFound(f"Market {market_id} not found")
        return event

    def get_market_onchain(self, market_id: str) -> OnchainMarketDTO:
        event = self.get_event(market_id)
        status_map = {"live": 1, "trading": 1, "finalized": 4, "voided": 5}
        code = status_map.get(event.status.lower(), 1)
        labels = {0: "Listed", 1: "Trading", 2: "Locked", 4: "Resolved", 5: "Voided"}
        return OnchainMarketDTO(
            market_id=event.market_id,
            status=code,
            status_label=labels.get(code, "Trading"),
            pool=event.pool_address or "",
            market_address=event.market_address or "",
            outcome_token=OUTCOME_TOKEN,
            yes_id=event.yes_token_id,
            no_id=event.no_token_id,
            expiry=event.expiry,
        )

    def get_order_book(self, yes_symbol: str, depth: int = 5) -> OrderBookDTO:
        event = next(
            (
                e
                for e in self._events.values()
                if e.yes_symbol == yes_symbol or e.market_id == yes_symbol
            ),
            None,
        )
        if not event:
            raise DreamDEXNotFound(f"No market for symbol {yes_symbol}")

        spread = Decimal("0.01")
        mid = event.yes_price
        bids = [
            OrderBookLevel(price=_quantize_price(mid - spread * i), quantity=Decimal(str(10 + i * 3)))
            for i in range(depth, 0, -1)
        ]
        asks = [
            OrderBookLevel(price=_quantize_price(mid + spread * i), quantity=Decimal(str(8 + i * 2)))
            for i in range(1, depth + 1)
        ]
        return OrderBookDTO(symbol=yes_symbol, bids=bids, asks=asks, mid_price=mid)

    def get_candles(self, pool: str, interval: int) -> list[CandleDTO]:
        event = next((e for e in self._events.values() if e.pool_address == pool), None)
        if not event:
            raise DreamDEXNotFound(f"No market for pool {pool}")

        now = django_tz.now()
        candles: list[CandleDTO] = []
        price = event.yes_price
        for i in range(12, 0, -1):
            ts = now - timedelta(seconds=interval * i)
            open_p = price - Decimal("0.02")
            close_p = price
            candles.append(
                CandleDTO(
                    timestamp=ts,
                    open=_quantize_price(open_p),
                    high=_quantize_price(max(open_p, close_p) + Decimal("0.01")),
                    low=_quantize_price(min(open_p, close_p) - Decimal("0.01")),
                    close=_quantize_price(close_p),
                    volume=Decimal(str(500 + i * 40)),
                )
            )
            price = open_p
        return candles

    def get_fills(
        self,
        pool: str,
        *,
        since: int | None = None,
        market_id: str | None = None,
    ) -> list[FillDTO]:
        fills = list(self._fills.get(pool, []))
        if not fills and pool:
            fills = list(self._fills.get(pool.lower(), []))
        if market_id:
            want = market_id.lower()
            fills = [f for f in fills if (f.market_id or "").lower() == want]
        if since is not None:
            cutoff = datetime.fromtimestamp(since, tz=timezone.utc)
            fills = [f for f in fills if f.timestamp >= cutoff]
        return sorted(fills, key=lambda f: f.timestamp, reverse=True)

    def get_recent_fills(self, *, limit: int = 300) -> list[FillDTO]:
        all_fills: list[FillDTO] = []
        for fills in self._fills.values():
            all_fills.extend(fills)
        return sorted(all_fills, key=lambda f: f.timestamp, reverse=True)[: max(int(limit), 0)]

    def get_user_fills(
        self,
        account: str,
        *,
        pool: str | None = None,
    ) -> list[FillDTO]:
        all_fills: list[FillDTO] = []
        pools = [pool] if pool else list(self._fills.keys())
        for p in pools:
            for fill in self._fills.get(p, []):
                if fill.maker.lower() == account.lower() or fill.taker.lower() == account.lower():
                    all_fills.append(fill)
        return sorted(all_fills, key=lambda f: f.timestamp, reverse=True)

    def get_outcome_balances(self, account: str, market_id: str) -> OutcomeBalancesDTO:
        self.get_event(market_id)
        wallet = account.lower()
        yes = Decimal("0")
        no = Decimal("0")
        for fills in self._fills.values():
            for fill in fills:
                if fill.market_id != market_id:
                    continue
                if fill.maker.lower() == wallet and fill.maker_side == "YES":
                    yes += fill.quantity
                elif fill.maker.lower() == wallet and fill.maker_side == "NO":
                    no += fill.quantity
                elif fill.taker.lower() == wallet and fill.taker_side == "YES":
                    yes += fill.quantity
                elif fill.taker.lower() == wallet and fill.taker_side == "NO":
                    no += fill.quantity
        redeemed = self._redeemed.get((wallet, market_id), {})
        yes = max(yes - redeemed.get("yes", Decimal("0")), Decimal("0"))
        no = max(no - redeemed.get("no", Decimal("0")), Decimal("0"))
        return OutcomeBalancesDTO(yes_balance=yes, no_balance=no)

    def get_wallet_balances(self, account: str) -> WalletBalancesDTO:
        network = (settings.DREAMDEX_NETWORK or "testnet").lower()
        return WalletBalancesDTO(
            address=account.lower(),
            native_balance=Decimal("12.5"),
            native_symbol="SOMI" if network == "mainnet" else "STT",
            collateral_balance=Decimal("250.00"),
            collateral_symbol="USDso" if network == "mainnet" else "Test USDC",
            collateral_address=COLLATERAL,
            chain_id=self._chain_id,
        )

    def prepare_place_order(self, intent: TradeIntent) -> UnsignedTxDTO:
        if intent.price <= 0 or intent.price >= 1:
            raise DreamDEXValidationError("Price must be in (0, 1)")
        if intent.quantity <= 0:
            raise DreamDEXValidationError("Quantity must be positive")

        event = self.get_event(intent.market_id)
        onchain = self.get_market_onchain(intent.market_id)
        if onchain.status != 1:
            raise DreamDEXValidationError(
                f"Market {intent.market_id} is not in Trading status (got {onchain.status_label})"
            )

        calldata = "0x" + secrets.token_hex(128)
        return UnsignedTxDTO(
            to=BINARY_POOL,
            data=calldata,
            value=0,
            chain_id=self._chain_id,
            description=f"Place {intent.side} order on {event.asset}",
            metadata={
                "marketId": intent.market_id,
                "pool": intent.pool or (event.pool_address or ""),
                "side": intent.side,
                "price": str(intent.price),
                "quantity": str(intent.quantity),
            },
        )

    def quote_wallet_fees(
        self,
        unsigned: UnsignedTxDTO,
        *,
        account: str,
        estimate: bool = True,
    ) -> None:
        unsigned.metadata["gas"] = "0x3d090"
        unsigned.metadata["maxFeePerGas"] = "0x2cb417800"
        unsigned.metadata["maxPriorityFeePerGas"] = "0x3b9aca00"

    def prepare_collateral_approval(
        self,
        *,
        account: str,
        spender: str,
        amount_raw: int,
        collateral: str | None = None,
    ) -> UnsignedTxDTO | None:
        return None

    def prepare_outcome_operator_approval(self, *, account: str) -> UnsignedTxDTO | None:
        return None

    def record_redeem(
        self,
        *,
        account: str,
        market_id: str,
        outcome_idx: int,
        amount_raw: int,
    ) -> None:
        decimals = int(getattr(settings, "DREAMDEX_COLLATERAL_DECIMALS", 6))
        human = (Decimal(int(amount_raw)) / (Decimal(10) ** decimals)).quantize(
            Decimal("0.000001")
        )
        side = "yes" if int(outcome_idx) == 0 else "no"
        key = (account.lower(), market_id)
        bag = self._redeemed.setdefault(key, {"yes": Decimal("0"), "no": Decimal("0")})
        bag[side] = bag.get(side, Decimal("0")) + human

    def list_finalized_events(
        self,
        *,
        venue_id: str | None = None,
    ) -> list[EventDTO]:
        events = list(self._finalized.values())
        if venue_id:
            events = [e for e in events if e.venue_id == venue_id]
        return events

    def prepare_redeem(
        self,
        *,
        market_id: str,
        account: str,
        outcome_idx: int,
        amount: int,
    ) -> UnsignedTxDTO:
        event = self.get_event(market_id)
        if event.status.lower() not in ("finalized", "voided", "resolved"):
            raise DreamDEXValidationError(f"Market {market_id} is not settled")

        calldata = "0x" + secrets.token_hex(64)
        return UnsignedTxDTO(
            to=OUTCOME_TOKEN,
            data=calldata,
            value=0,
            chain_id=self._chain_id,
            description=f"Redeem outcome {outcome_idx} for {event.asset}",
            metadata={
                "marketId": market_id,
                "account": account,
                "outcomeIdx": str(outcome_idx),
                "amount": str(amount),
            },
        )

    def bump_prices(self, delta: Decimal = Decimal("0.01")) -> None:
        """Shift live event YES prices slightly (for sync simulation)."""
        updated: dict[str, EventDTO] = {}
        for market_id, event in self._events.items():
            new_yes = min(max(event.yes_price + delta, Decimal("0.05")), Decimal("0.95"))
            yes, no = _split_prices(new_yes)
            history = self._price_history.setdefault(market_id, [event.yes_price])
            history.append(new_yes)
            updated[market_id] = replace(
                event,
                yes_price=yes,
                no_price=no,
                last_price=yes,
            )
        self._events.update(updated)

    def get_price_momentum(self, market_id: str) -> Decimal:
        """Return recent price change for radar scoring."""
        history = self._price_history.get(market_id, [])
        if len(history) < 2:
            return Decimal("0")
        return history[-1] - history[-2]

    def simulate_settlement(self, market_id: str, winning_outcome: str = "YES") -> EventDTO:
        """Move a live market to finalized state for demo settlement flows."""
        event = self._events.pop(market_id, None)
        if not event:
            raise DreamDEXNotFound(f"Live market {market_id} not found")

        settled = replace(
            event,
            status="Finalized",
            winning_outcome=winning_outcome,
        )
        self._finalized[market_id] = settled
        return settled

    def simulate_lock(self, market_id: str) -> EventDTO:
        """Expire a live market without a posted winner (oracle still settling)."""
        event = self._events.pop(market_id, None)
        if not event:
            raise DreamDEXNotFound(f"Live market {market_id} not found")
        locked = replace(
            event,
            status="Locked",
            expiry=django_tz.now() - timedelta(minutes=1),
            winning_outcome=None,
        )
        self._finalized[market_id] = locked
        return locked

    def simulate_void(self, market_id: str) -> EventDTO:
        event = self._events.pop(market_id, None)
        if not event:
            raise DreamDEXNotFound(f"Live market {market_id} not found")
        voided = replace(event, status="Voided", winning_outcome=None)
        self._finalized[market_id] = voided
        return voided

    def get_trader_wallets(self) -> dict[str, str]:
        return dict(TRADER_WALLETS)
