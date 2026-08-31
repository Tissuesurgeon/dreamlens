"""Live DreamDEX Event Contract client (GraphQL indexer + RPC)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from django.conf import settings
from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address
from web3 import Web3

from integrations.dreamdex.exceptions import (
    DreamDEXNotFound,
    DreamDEXUnavailable,
    DreamDEXValidationError,
)
from integrations.dreamdex.types import (
    CandleDTO,
    EventDTO,
    FillDTO,
    OnchainMarketDTO,
    OrderBookDTO,
    OrderBookLevel,
    OutcomeBalancesDTO,
    OutcomeSide,
    TradeIntent,
    UnsignedTxDTO,
    WalletBalancesDTO,
)

logger = logging.getLogger("dreamlens.dreamdex.client")

_MARKET_SELECTION = """
  id
  marketId
  marketType
  poolAddress
  binaryPoolAddress
  marketAddress
  lastPrice
  cumulativeQuoteVolume
  cumulativeBaseVolume
  tradeCount
  quoteDecimals
  asset
  strike
  intervalSec
  expiry
  tradingStart
  yesTokenId
  noTokenId
  clobStatus
  venueId
  collateral
  question
  voided
  winningOutcome
  oracleQuestionId
"""

_STATUS_ONCHAIN = {
    0: "Listed",
    1: "Trading",
    2: "Locked",
    3: "Settling",
    4: "Resolved",
    5: "Voided",
}

_SIDE_TO_KIND = {
    "BUY_YES": 0,
    "SELL_YES": 1,
    "BUY_NO": 2,
    "SELL_NO": 3,
}

# Matches @somnia-chain/markets-sdk ORDER_TYPE (OrderBook enum):
# 0 NormalOrder, 1 FillOrKill, 2 ImmediateOrCancel, 3 PostOnly.
# Encoding MARKET as 1 (FOK) makes MetaMask eth_estimateGas revert
# FillOrKillNotFillable — the wallet then shows "Network fee: Unavailable".
_ORDER_TYPE = {
    "LIMIT": 0,
    "FILL_OR_KILL": 1,
    "MARKET": 2,
    "POST_ONLY": 3,
}

_PLACE_BINARY_TYPES = [
    "uint8",
    "uint256",
    "uint256",
    "uint64",
    "uint8",
    "uint8",
    "address",
    "uint96",
    "uint64",
]

_PLACE_BINARY_SIGNATURE = (
    "placeBinaryOrder(uint8,uint256,uint256,uint64,uint8,uint8,address,uint96,uint64)"
)

# Bare 4-byte selectors from the Somnia OrderBook / BinaryPool ABI.
_PLACE_ORDER_REVERTS = {
    "c04ad919": "This market cannot fill the full size right now. Try a smaller amount.",
    "d48c4403": "No liquidity to take on this market right now. Try again in a moment.",
    "3154078e": "This order's expiry is in the past. Refresh and try again.",
    "d3dea628": "This order expires after the market. Refresh and try a later window.",
    "00bfc921": "Price is off the venue tick grid.",
    "524f409b": "Quantity is off the venue lot grid.",
    "4f174b29": "Order size is off this market's lot grid. Try a slightly different amount.",
    "f4d678b8": "Not enough outcome tokens to sell.",
    "fb8f41b2": "USDC is not approved for this market pool. Confirm the approval in MetaMask first.",
    "e450d38c": "Not enough Test USDC in this wallet to cover the order.",
}


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _ts(value: Any) -> datetime:
    raw = int(Decimal(str(value or 0)))
    return datetime.fromtimestamp(raw, tz=UTC)


def _human_price(raw: Any, decimals: int) -> Decimal:
    """Convert indexer raw price to YES probability in (0, 1)."""
    if raw is None or raw == "":
        return Decimal("0.5")
    scale = Decimal(10) ** decimals
    price = _dec(raw) / scale
    if price <= 0:
        return Decimal("0.5")
    if price > 1:
        # Guard against unexpected scaling
        return min(price / scale, Decimal("0.99"))
    return price.quantize(Decimal("0.000001"))


def _human_amount(raw: Any, decimals: int) -> Decimal:
    scale = Decimal(10) ** decimals
    return (_dec(raw) / scale).quantize(Decimal("0.000001"))


def _outcome_symbol(asset: str, market_id: str, side: str) -> str:
    short = market_id[-8:] if market_id else "market"
    return f"{asset}-{short}/USDso#{side}"


def _marketable_yes_price_raw(side: str, *, decimals: int, tick: int) -> int:
    """IOC/market limit in YES-price units so a taker order can cross the book.

    Price is always the YES-side price. A market BUY_YES (or SELL_NO) must cap
    at almost 1; a market BUY_NO (or SELL_YES) must floor at one tick.
    """
    scale = 10 ** int(decimals)
    step = max(int(tick), 1)
    if side in {"BUY_YES", "SELL_NO"}:
        return scale - step
    return step


def align_quantity_raw(qty_raw: int, *, lot: int, min_qty: int) -> int:
    """Snap size down to lot and reject dust below the pool minimum."""
    lot = max(int(lot or 1), 1)
    min_qty = max(int(min_qty or 0), lot)
    aligned = (int(qty_raw) // lot) * lot
    if aligned < min_qty:
        raise DreamDEXValidationError(
            "Amount is below this market's minimum size. Try a larger trade."
        )
    return aligned


def encode_place_binary_order(
    *,
    kind: int,
    price_raw: int,
    qty_raw: int,
    expire_ns: int,
    order_type: int,
    self_matching_option: int = 0,
    builder: str = "0x0000000000000000000000000000000000000000",
    builder_fee: int = 0,
    user_data: int = 0,
) -> str:
    selector = function_signature_to_4byte_selector(_PLACE_BINARY_SIGNATURE)
    encoded = encode(
        _PLACE_BINARY_TYPES,
        [
            int(kind),
            int(price_raw),
            int(qty_raw),
            int(expire_ns),
            int(order_type),
            int(self_matching_option),
            builder,
            int(builder_fee),
            int(user_data),
        ],
    )
    return "0x" + (selector + encoded).hex()


_REDEEM_SIGNATURE = "redeem(uint32,bytes32,bytes32,uint8,uint256)"
_REDEEM_TYPES = ["uint32", "bytes32", "bytes32", "uint8", "uint256"]
_OUTCOME_TOKEN_6909 = "0xB52c5934113Af5c0Bb20eb3C72290C8215f755b9"
_SETTLED_CLOB = {"finalized", "resolved", "voided"}


def _bytes32(value: str) -> bytes:
    raw = (value or "").strip()
    if raw.startswith(("0x", "0X")):
        raw = raw[2:]
    if not raw:
        return b"\x00" * 32
    data = bytes.fromhex(raw)
    if len(data) > 32:
        data = data[-32:]
    return data.rjust(32, b"\x00")


def _winning_side(raw: Any) -> OutcomeSide | None:
    """Indexer winningOutcome is 0/1; tolerate YES/NO labels if the field type shifts."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        label = raw.strip().upper()
        if label in {"YES", "UP"}:
            return "YES"
        if label in {"NO", "DOWN"}:
            return "NO"
    try:
        return "YES" if int(raw) == 0 else "NO"
    except (TypeError, ValueError):
        return None


def encode_module_redeem(
    *,
    market_id: str,
    amount: int,
    outcome_idx: int,
    venue_id: str = "",
    operator_id: int = 0,
) -> str:
    """BinaryMarketsModule.redeem from @somnia-chain/markets-sdk binaryModuleWriteAbi."""
    selector = function_signature_to_4byte_selector(_REDEEM_SIGNATURE)
    encoded = encode(
        _REDEEM_TYPES,
        [
            int(operator_id),
            _bytes32(venue_id),
            _bytes32(market_id),
            int(outcome_idx),
            int(amount),
        ],
    )
    return "0x" + (selector + encoded).hex()


def encode_finalize_market(market_id: str) -> str:
    selector = function_signature_to_4byte_selector("finalizeMarket(bytes32)")
    encoded = encode(["bytes32"], [_bytes32(market_id)])
    return "0x" + (selector + encoded).hex()


def encode_poke_oracle(oracle_question_id: int) -> str:
    selector = function_signature_to_4byte_selector("pokeOracle(uint256)")
    encoded = encode(["uint256"], [int(oracle_question_id)])
    return "0x" + (selector + encoded).hex()


def encode_sync_settlement(market_id: str) -> str:
    selector = function_signature_to_4byte_selector("syncSettlement(bytes32)")
    encoded = encode(["bytes32"], [_bytes32(market_id)])
    return "0x" + (selector + encoded).hex()


def humanize_place_order_revert(exc: BaseException) -> str:
    blob = " ".join(str(part) for part in (exc, *getattr(exc, "args", ()))).lower()
    data = getattr(exc, "data", None)
    if data is not None:
        blob = f"{blob} {data}".lower()
    hex_blob = blob.replace("0x", "")
    for selector, message in _PLACE_ORDER_REVERTS.items():
        if selector in hex_blob:
            return message
    return "DreamDEX rejected this order. Refresh the market and try again."


def explain_reverted_tx(tx_hash: str) -> str:
    """Replay a mined-but-reverted tx so the user sees the pool error, not a generic fail."""
    rpc = getattr(settings, "DREAMDEX_RPC_URL", "") or ""
    w3 = Web3(Web3.HTTPProvider(rpc))
    tx = w3.eth.get_transaction(tx_hash)
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    block = max(int(receipt["blockNumber"]) - 1, 0)
    data = tx.get("input") or tx.get("data") or "0x"
    if hasattr(data, "hex") and not isinstance(data, str):
        data = "0x" + data.hex()
    try:
        w3.eth.call(
            {
                "from": tx["from"],
                "to": tx["to"],
                "data": data,
                "value": int(tx.get("value") or 0),
            },
            block,
        )
    except Exception as exc:  # noqa: BLE001
        return humanize_place_order_revert(exc)
    return "On-chain transaction failed"


def _map_side(raw: Any) -> OutcomeSide:
    text = str(raw or "").upper()
    if "NO" in text or text.endswith("_NO"):
        return "NO"
    return "YES"


def _is_buy_side(raw: Any) -> bool:
    text = str(raw or "").upper()
    return not (text.startswith("SELL") or text.startswith("ASK"))


def _parse_opening_price(answer: dict[str, Any]) -> Decimal | None:
    """
    Opening / reference USD price from the oracle answer tied to MarketReferenceLink.

    Prefer outcomeLabel like '>= 76888.7200'. Fall back to numericValue / 100
    (observed scale on Shannon testnet reference answers).
    """
    label = str(answer.get("outcomeLabel") or "")
    match = re.search(r">=\s*([\d,]+(?:\.\d+)?)", label)
    if match:
        try:
            value = Decimal(match.group(1).replace(",", ""))
            if value > 0:
                return value.quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            pass

    raw = answer.get("numericValue")
    if raw is None or raw == "":
        return None
    try:
        numeric = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if numeric <= 0:
        return None
    return (numeric / Decimal("100")).quantize(Decimal("0.01"))


class LiveDreamDEXClient:
    """Production adapter against the Somnia Markets GraphQL indexer + RPC."""

    def __init__(self) -> None:
        self.indexer_url = settings.DREAMDEX_INDEXER_URL
        self.rpc_url = settings.DREAMDEX_RPC_URL
        self.chain_id = int(settings.DREAMDEX_CHAIN_ID)
        self.venue_id = (settings.DREAMDEX_VENUE_ID or "").strip()
        self.decimals = int(settings.DREAMDEX_COLLATERAL_DECIMALS)
        self.tick = int(settings.DREAMDEX_TICK)
        self.lot = int(settings.DREAMDEX_LOT)
        self.binary_module = (
            settings.DREAMDEX_BINARY_MODULE
            or "0x3ecC694Cef705358864a646142ac17A90E29e388"
        )
        self._http = httpx.Client(timeout=45.0)
        self._web3 = Web3(Web3.HTTPProvider(self.rpc_url))

    # ------------------------------------------------------------------ reads

    def list_events(
        self,
        *,
        venue_id: str | None = None,
        status: str | None = None,
    ) -> list[EventDTO]:
        venue = (venue_id if venue_id is not None else self.venue_id) or None
        status_norm = (status or "live").lower()
        if status_norm in {"live", "trading"}:
            clob = "Trading"
            limit = 50
        elif status_norm in {"finalized", "resolved", "voided"}:
            clob = "Finalized"
            limit = 25
        else:
            clob = status
            limit = 50

        where: dict[str, Any] = {
            "marketType": {"_eq": "BINARY"},
            "clobStatus": {"_eq": clob},
        }
        if venue:
            where["venueId"] = {"_eq": venue}
        if clob == "Trading":
            # Prefer markets that have not expired yet (unix seconds).
            where["expiry"] = {"_gt": str(int(datetime.now(tz=UTC).timestamp()))}

        order_by = {"expiry": "asc"} if clob == "Trading" else {"expiry": "desc"}

        query = f"""
        query($where: Market_bool_exp!, $limit: Int!, $order_by: [Market_order_by!]) {{
          Market(where: $where, limit: $limit, order_by: $order_by) {{
            {_MARKET_SELECTION}
          }}
        }}
        """
        data = self._graphql(
            query,
            {"where": where, "limit": limit, "order_by": [order_by]},
        )
        rows = data.get("Market") or []
        opening_by_market = self._opening_prices_for_markets(
            [str(row.get("marketId") or row.get("id") or "") for row in rows]
        )
        return [
            self._row_to_event(
                row,
                opening_price=opening_by_market.get(
                    str(row.get("marketId") or row.get("id") or "")
                ),
            )
            for row in rows
        ]

    def get_event(self, market_id: str) -> EventDTO:
        query = f"""
        query($id: String!) {{
          Market(where: {{marketId: {{_eq: $id}}, marketType: {{_eq: BINARY}}}}, limit: 1) {{
            {_MARKET_SELECTION}
          }}
        }}
        """
        data = self._graphql(query, {"id": market_id})
        rows = data.get("Market") or []
        if not rows:
            raise DreamDEXNotFound(f"Event Contract not found: {market_id}")
        opening = self._opening_prices_for_markets([market_id]).get(market_id)
        return self._row_to_event(rows[0], opening_price=opening)

    def list_finalized_events(self, *, venue_id: str | None = None) -> list[EventDTO]:
        return self.list_events(venue_id=venue_id, status="Finalized")

    def get_market_onchain(self, market_id: str) -> OnchainMarketDTO:
        """Prefer indexer row; fall back to RPC status labels from clobStatus map."""
        event = self.get_event(market_id)
        status_label = event.status
        status_code = next(
            (code for code, label in _STATUS_ONCHAIN.items() if label.lower() == status_label.lower()),
            1 if status_label.lower() == "trading" else 0,
        )
        return OnchainMarketDTO(
            market_id=event.market_id,
            status=status_code,
            status_label=_STATUS_ONCHAIN.get(status_code, status_label),
            pool=event.pool_address or "",
            market_address=event.market_address or "",
            outcome_token=_OUTCOME_TOKEN_6909,
            yes_id=event.yes_token_id,
            no_id=event.no_token_id,
            expiry=event.expiry,
        )

    def get_order_book(self, yes_symbol: str, depth: int = 5) -> OrderBookDTO:
        market_id = self._market_id_from_symbol(yes_symbol)
        query = """
        query($id: String!, $limit: Int!) {
          Order(
            where: {
              market_id: {_eq: $id},
              status: {_eq: "Open"},
              rested: {_eq: true},
              quantityRemaining: {_gt: "0"}
            }
            limit: $limit
            order_by: {placedAtTimestamp: desc}
          ) {
            side price quantityRemaining isBid
          }
        }
        """
        data = self._graphql(query, {"id": market_id, "limit": max(depth * 10, 40)})
        bids: list[OrderBookLevel] = []
        asks: list[OrderBookLevel] = []
        for row in data.get("Order") or []:
            price = _human_price(row.get("price"), self.decimals)
            qty = _human_amount(row.get("quantityRemaining"), self.decimals)
            level = OrderBookLevel(price=price, quantity=qty)
            side = str(row.get("side") or "")
            is_bid = bool(row.get("isBid"))
            if side.startswith("BUY") or is_bid:
                bids.append(level)
            else:
                asks.append(level)

        bids = sorted(bids, key=lambda x: x.price, reverse=True)[:depth]
        asks = sorted(asks, key=lambda x: x.price)[:depth]
        mid = None
        if bids and asks:
            mid = ((bids[0].price + asks[0].price) / 2).quantize(Decimal("0.000001"))
        elif bids:
            mid = bids[0].price
        elif asks:
            mid = asks[0].price
        return OrderBookDTO(symbol=yes_symbol, bids=bids, asks=asks, mid_price=mid)

    def get_candles(self, pool: str, interval: int) -> list[CandleDTO]:
        query = """
        query($pool: String!, $interval: Int!, $limit: Int!) {
          Candle(
            where: {pool: {_eq: $pool}, intervalSeconds: {_eq: $interval}}
            limit: $limit
            order_by: {bucketStart: desc}
          ) {
            bucketStart openPrice high low closePrice quoteVolume
          }
        }
        """
        data = self._graphql(
            query,
            {"pool": pool.lower(), "interval": int(interval), "limit": 120},
        )
        candles: list[CandleDTO] = []
        for row in reversed(data.get("Candle") or []):
            candles.append(
                CandleDTO(
                    timestamp=_ts(row.get("bucketStart")),
                    open=_human_price(row.get("openPrice"), self.decimals),
                    high=_human_price(row.get("high"), self.decimals),
                    low=_human_price(row.get("low"), self.decimals),
                    close=_human_price(row.get("closePrice"), self.decimals),
                    volume=_human_amount(row.get("quoteVolume"), self.decimals),
                )
            )
        return candles

    def get_fills(
        self,
        pool: str,
        *,
        since: int | None = None,
        market_id: str | None = None,
    ) -> list[FillDTO]:
        pool_l = (pool or "").lower()
        where: dict[str, Any] = {
            "_or": [
                {"pool": {"_eq": pool_l}},
                {"pool": {"_eq": pool}},
            ]
        }
        clauses: list[dict[str, Any]] = [where]
        if market_id:
            clauses.append({"market_id": {"_eq": market_id}})
        if since is not None:
            clauses.append({"timestamp": {"_gte": str(since)}})
        if len(clauses) > 1:
            where = {"_and": clauses}
        query = """
        query($where: Fill_bool_exp!, $limit: Int!) {
          Fill(where: $where, limit: $limit, order_by: {timestamp: desc}) {
            id
            market_id
            pool
            fillPrice
            quantity
            quoteQuantity
            maker
            taker
            makerSide
            takerSide
            kind
            takerIsBid
            timestamp
            txHash
          }
        }
        """
        data = self._graphql(query, {"where": where, "limit": 200 if market_id else 100})
        return [self._row_to_fill(row) for row in (data.get("Fill") or [])]

    def get_recent_fills(self, *, limit: int = 300) -> list[FillDTO]:
        query = """
        query($limit: Int!) {
          Fill(where: {}, limit: $limit, order_by: {timestamp: desc}) {
            id
            market_id
            pool
            fillPrice
            quantity
            quoteQuantity
            maker
            taker
            makerSide
            takerSide
            kind
            takerIsBid
            timestamp
            txHash
          }
        }
        """
        data = self._graphql(query, {"limit": max(int(limit), 1)})
        return [self._row_to_fill(row) for row in (data.get("Fill") or [])]

    def get_user_fills(
        self,
        account: str,
        *,
        pool: str | None = None,
    ) -> list[FillDTO]:
        variants = {account.strip(), account.strip().lower()}
        try:
            variants.add(to_checksum_address(account))
        except Exception:
            pass
        or_clause: list[dict[str, Any]] = []
        for value in variants:
            if not value:
                continue
            or_clause.append({"maker": {"_eq": value}})
            or_clause.append({"taker": {"_eq": value}})
        if not or_clause:
            return []
        where: dict[str, Any] = {"_or": or_clause}
        if pool:
            where["pool"] = {"_eq": pool.lower()}
        query = """
        query($where: Fill_bool_exp!, $limit: Int!) {
          Fill(where: $where, limit: $limit, order_by: {timestamp: desc}) {
            id
            market_id
            pool
            fillPrice
            quantity
            quoteQuantity
            maker
            taker
            makerSide
            takerSide
            kind
            takerIsBid
            timestamp
            txHash
          }
        }
        """
        data = self._graphql(query, {"where": where, "limit": 500})
        seen: set[str] = set()
        fills: list[FillDTO] = []
        for row in data.get("Fill") or []:
            fill = self._row_to_fill(row)
            if fill.id in seen:
                continue
            seen.add(fill.id)
            fills.append(fill)
        return fills

    def get_outcome_balances(self, account: str, market_id: str) -> OutcomeBalancesDTO:
        onchain = self.get_market_onchain(market_id)
        outcome_token = to_checksum_address(_OUTCOME_TOKEN_6909)
        erc6909_abi = [
            {
                "name": "balanceOf",
                "type": "function",
                "stateMutability": "view",
                "inputs": [
                    {"name": "owner", "type": "address"},
                    {"name": "id", "type": "uint256"},
                ],
                "outputs": [{"name": "", "type": "uint256"}],
            }
        ]
        contract = self._web3.eth.contract(address=outcome_token, abi=erc6909_abi)
        owner = to_checksum_address(account)
        yes_raw = contract.functions.balanceOf(owner, int(onchain.yes_id)).call()
        no_raw = contract.functions.balanceOf(owner, int(onchain.no_id)).call()
        return {
            "yes_balance": _human_amount(yes_raw, self.decimals),
            "no_balance": _human_amount(no_raw, self.decimals),
        }

    def get_wallet_balances(self, account: str) -> WalletBalancesDTO:
        """Native gas balance + DreamDEX collateral ERC-20 balance."""
        owner = to_checksum_address(account)
        network = (settings.DREAMDEX_NETWORK or "testnet").lower()
        native_symbol = "SOMI" if network == "mainnet" else "STT"
        collateral_symbol = "USDso" if network == "mainnet" else "Test USDC"
        collateral = self._collateral_address(None)
        decimals = int(self.decimals)

        try:
            native_wei = int(self._web3.eth.get_balance(owner))
        except Exception as exc:  # noqa: BLE001
            logger.warning("native balance read failed: %s", exc)
            raise DreamDEXUnavailable(f"Could not read native balance: {exc}") from exc

        erc20 = self._web3.eth.contract(
            address=collateral,
            abi=[
                {
                    "name": "balanceOf",
                    "type": "function",
                    "stateMutability": "view",
                    "inputs": [{"name": "account", "type": "address"}],
                    "outputs": [{"name": "", "type": "uint256"}],
                },
            ],
        )
        try:
            coll_raw = int(erc20.functions.balanceOf(owner).call())
        except Exception as exc:  # noqa: BLE001
            logger.warning("collateral balance read failed: %s", exc)
            raise DreamDEXUnavailable(f"Could not read collateral balance: {exc}") from exc

        scale_native = Decimal(10) ** 18
        scale_coll = Decimal(10) ** decimals
        return WalletBalancesDTO(
            address=owner.lower(),
            native_balance=(Decimal(native_wei) / scale_native).quantize(Decimal("0.0001")),
            native_symbol=native_symbol,
            collateral_balance=(Decimal(coll_raw) / scale_coll).quantize(Decimal("0.01")),
            collateral_symbol=collateral_symbol,
            collateral_address=collateral.lower(),
            chain_id=self.chain_id,
        )

    def _book_params(self, pool: str) -> tuple[int, int, int]:
        """Return (tick, min_quantity, lot) from the pool, falling back to settings."""
        tick = max(int(self.tick or 1), 1)
        lot = max(int(self.lot or 1), 1)
        min_qty = lot
        try:
            selector = function_signature_to_4byte_selector("getOrderBookParameters()")
            raw = self._web3.eth.call(
                {"to": to_checksum_address(pool), "data": "0x" + selector.hex()}
            )
            decoded = decode(["uint256", "uint256", "uint256"], bytes(raw))
            tick = max(int(decoded[0]), 1)
            min_qty = max(int(decoded[1]), 1)
            lot = max(int(decoded[2]), 1)
        except Exception:
            logger.warning("getOrderBookParameters failed pool=%s", pool, exc_info=True)
        return tick, min_qty, lot

    def prepare_place_order(self, intent: TradeIntent) -> UnsignedTxDTO:
        kind = _SIDE_TO_KIND.get(intent.side)
        if kind is None:
            raise DreamDEXUnavailable(f"Unsupported side: {intent.side}")

        tick, min_qty, lot = self._book_params(intent.pool)
        order_type = _ORDER_TYPE.get(intent.order_type, _ORDER_TYPE["LIMIT"])
        if intent.order_type == "MARKET":
            price_raw = _marketable_yes_price_raw(
                intent.side, decimals=self.decimals, tick=tick
            )
        else:
            price_raw = int((intent.price * (Decimal(10) ** self.decimals)).to_integral_value())
            price_raw = (price_raw // tick) * tick
        qty_raw = int((intent.quantity * (Decimal(10) ** self.decimals)).to_integral_value())
        qty_raw = align_quantity_raw(qty_raw, lot=lot, min_qty=min_qty)
        if qty_raw <= 0 or price_raw <= 0:
            raise DreamDEXUnavailable("Price or quantity below venue tick/lot grid")

        expire_ns = intent.expire_timestamp_ns
        if expire_ns is None:
            expire_ns = int(datetime.now(tz=UTC).timestamp() + 300) * 1_000_000_000

        data = encode_place_binary_order(
            kind=kind,
            price_raw=price_raw,
            qty_raw=qty_raw,
            expire_ns=int(expire_ns),
            order_type=order_type,
        )
        return UnsignedTxDTO(
            to=to_checksum_address(intent.pool),
            data=data,
            value=0,
            chain_id=self.chain_id,
            description=f"placeBinaryOrder {intent.side} on {intent.market_id}",
            metadata={
                "market_id": intent.market_id,
                "side": intent.side,
                "order_type": intent.order_type,
                "price_raw": str(price_raw),
                "quantity_raw": str(qty_raw),
                "collateral": self._collateral_address(None),
            },
        )

    def simulate_unsigned_tx(self, unsigned: UnsignedTxDTO, *, account: str) -> None:
        """eth_call the prepared order so MetaMask is not asked to sign a reverting tx."""
        if not account or not unsigned.to or not unsigned.data:
            return
        tx = {
            "from": to_checksum_address(account),
            "to": to_checksum_address(unsigned.to),
            "data": unsigned.data,
            "value": int(unsigned.value or 0),
        }
        try:
            self._web3.eth.call(tx)
        except Exception as exc:  # noqa: BLE001 — RPC revert payload is the signal
            raise DreamDEXValidationError(humanize_place_order_revert(exc)) from exc

    def quote_wallet_fees(
        self,
        unsigned: UnsignedTxDTO,
        *,
        account: str,
        estimate: bool = True,
    ) -> None:
        """Attach gas + EIP-1559 fees so MetaMask can show a network fee on Shannon.

        Somnia feeHistory often reports 0 priority rewards; without explicit
        maxPriorityFeePerGas MetaMask shows "Network fee: Unavailable".
        """
        if not unsigned.to:
            return
        try:
            gas_price = int(self._web3.eth.gas_price or 6_000_000_000)
        except Exception:
            gas_price = 6_000_000_000
        priority = max(gas_price // 10, 1_000_000_000)
        max_fee = max(gas_price * 2, gas_price + priority)
        unsigned.metadata["maxFeePerGas"] = hex(max_fee)
        unsigned.metadata["maxPriorityFeePerGas"] = hex(priority)
        if estimate and account:
            tx = {
                "from": to_checksum_address(account),
                "to": to_checksum_address(unsigned.to),
                "data": unsigned.data,
                "value": int(unsigned.value or 0),
            }
            try:
                gas = int(self._web3.eth.estimate_gas(tx))
            except Exception as exc:  # noqa: BLE001
                raise DreamDEXValidationError(humanize_place_order_revert(exc)) from exc
            unsigned.metadata["gas"] = hex(max(int(gas * 125 // 100), gas + 50_000))
        else:
            unsigned.metadata.setdefault("gas", hex(800_000))

    def _collateral_address(self, explicit: str | None) -> str:
        raw = (
            explicit
            or settings.DREAMDEX_COLLATERAL
            or "0x70a86D8842FB63C4Ad2b7cdddF530eBf1BB25d8E"
        )
        return to_checksum_address(raw)

    def prepare_collateral_approval(
        self,
        *,
        account: str,
        spender: str,
        amount_raw: int,
        collateral: str | None = None,
    ) -> UnsignedTxDTO | None:
        """Return an ERC-20 approve tx when the pool cannot pull enough collateral."""
        token = self._collateral_address(collateral)
        owner = to_checksum_address(account)
        pool = to_checksum_address(spender)
        erc20 = self._web3.eth.contract(
            address=token,
            abi=[
                {
                    "name": "allowance",
                    "type": "function",
                    "stateMutability": "view",
                    "inputs": [
                        {"name": "owner", "type": "address"},
                        {"name": "spender", "type": "address"},
                    ],
                    "outputs": [{"name": "", "type": "uint256"}],
                },
            ],
        )
        try:
            allowance = int(erc20.functions.allowance(owner, pool).call())
        except Exception as exc:  # noqa: BLE001 — RPC/token may be unreachable
            logger.warning("allowance check failed: %s", exc)
            allowance = 0
        if allowance >= max(int(amount_raw), 1):
            return None

        selector = function_signature_to_4byte_selector("approve(address,uint256)")
        encoded = encode(["address", "uint256"], [pool, 2**256 - 1])
        return UnsignedTxDTO(
            to=token,
            data="0x" + (selector + encoded).hex(),
            value=0,
            chain_id=self.chain_id,
            description=f"Approve {token} for DreamDEX pool {pool}",
            metadata={"spender": pool, "collateral": token},
        )

    def prepare_outcome_operator_approval(
        self, *, account: str, spender: str | None = None
    ) -> UnsignedTxDTO | None:
        """ERC-6909 setOperator so a DreamDEX contract can pull outcome tokens.

        Redeem uses BinaryMarketsModule. Selling into the CLOB uses the market pool.
        """
        token = to_checksum_address(_OUTCOME_TOKEN_6909)
        operator = to_checksum_address(spender or self.binary_module)
        owner = to_checksum_address(account)
        erc6909 = self._web3.eth.contract(
            address=token,
            abi=[
                {
                    "name": "isOperator",
                    "type": "function",
                    "stateMutability": "view",
                    "inputs": [
                        {"name": "owner", "type": "address"},
                        {"name": "spender", "type": "address"},
                    ],
                    "outputs": [{"name": "", "type": "bool"}],
                }
            ],
        )
        try:
            if bool(erc6909.functions.isOperator(owner, operator).call()):
                return None
        except Exception:
            logger.warning("isOperator check failed account=%s", account, exc_info=True)
        selector = function_signature_to_4byte_selector("setOperator(address,bool)")
        encoded = encode(["address", "bool"], [operator, True])
        redeeming = operator.lower() == to_checksum_address(self.binary_module).lower()
        return UnsignedTxDTO(
            to=token,
            data="0x" + (selector + encoded).hex(),
            value=0,
            chain_id=self.chain_id,
            description=(
                "Allow DreamDEX to redeem outcome tokens"
                if redeeming
                else "Allow this market to sell your outcome tokens"
            ),
            metadata={"spender": operator, "token": token},
        )

    def prepare_redeem(
        self,
        *,
        market_id: str,
        account: str,
        outcome_idx: int,
        amount: int,
    ) -> UnsignedTxDTO:
        event = self.get_event(market_id)
        if (event.status or "").lower() not in _SETTLED_CLOB and not event.winning_outcome:
            raise DreamDEXValidationError(
                f"Market {market_id} is not settled yet. Winnings cannot be claimed."
            )
        if int(amount) <= 0:
            raise DreamDEXValidationError("Redeem amount must be positive")
        data = encode_module_redeem(
            market_id=market_id,
            amount=int(amount),
            outcome_idx=int(outcome_idx),
            venue_id=event.venue_id or self.venue_id,
            operator_id=int(getattr(event, "operator_id", 0) or 0),
        )
        return UnsignedTxDTO(
            to=to_checksum_address(self.binary_module),
            data=data,
            value=0,
            chain_id=self.chain_id,
            description=f"redeem outcome {outcome_idx} for {market_id}",
            metadata={
                "account": account,
                "market_id": market_id,
                "outcome_idx": str(outcome_idx),
                "amount": str(amount),
                "gas": hex(10_000_000),
            },
        )

    def read_settlement_ready(self, market_id: str) -> dict[str, Any]:
        """On-chain isResolved / isVoided plus oracle id for pokeOracle."""
        module = to_checksum_address(self.binary_module)
        mid = _bytes32(market_id)
        markets_abi = [
            {
                "name": "markets",
                "type": "function",
                "stateMutability": "view",
                "inputs": [{"name": "marketId", "type": "bytes32"}],
                "outputs": [
                    {"name": "oracleQuestionId", "type": "uint256"},
                    {"name": "outcomeSlotCount", "type": "uint8"},
                    {"name": "voidPolicy", "type": "uint8"},
                    {"name": "collateral", "type": "address"},
                    {"name": "originOperatorId", "type": "uint32"},
                    {"name": "originVenueId", "type": "bytes32"},
                    {"name": "oracleAdapter", "type": "address"},
                    {"name": "creator", "type": "address"},
                    {"name": "market", "type": "address"},
                    {"name": "pool", "type": "address"},
                    {"name": "yesId", "type": "uint256"},
                    {"name": "noId", "type": "uint256"},
                    {"name": "tradingStart", "type": "uint64"},
                    {"name": "expiry", "type": "uint64"},
                ],
            }
        ]
        rec = self._web3.eth.contract(address=module, abi=markets_abi).functions.markets(mid).call()
        market_addr = rec[8]
        if isinstance(market_addr, (bytes, bytearray)):
            market_hex = "0x" + bytes(market_addr).hex()
        else:
            market_hex = str(market_addr)
        if int(market_hex, 16) == 0:
            return {
                "is_resolved": False,
                "is_voided": False,
                "oracle_question_id": int(rec[0] or 0),
                "operator_id": int(rec[4] or 0),
                "venue_id": "0x" + (rec[5].hex() if isinstance(rec[5], (bytes, bytearray)) else ""),
            }
        view_abi = [
            {
                "name": "isResolved",
                "type": "function",
                "stateMutability": "view",
                "inputs": [],
                "outputs": [{"name": "", "type": "bool"}],
            },
            {
                "name": "isVoided",
                "type": "function",
                "stateMutability": "view",
                "inputs": [],
                "outputs": [{"name": "", "type": "bool"}],
            },
        ]
        market = self._web3.eth.contract(
            address=to_checksum_address(market_hex), abi=view_abi
        )
        venue = rec[5]
        venue_hex = (
            "0x" + venue.hex()
            if isinstance(venue, (bytes, bytearray))
            else str(venue or "")
        )
        return {
            "is_resolved": bool(market.functions.isResolved().call()),
            "is_voided": bool(market.functions.isVoided().call()),
            "oracle_question_id": int(rec[0] or 0),
            "operator_id": int(rec[4] or 0),
            "venue_id": venue_hex,
            "market_address": to_checksum_address(market_hex),
        }

    def prepare_finalize_market(self, market_id: str) -> UnsignedTxDTO:
        return UnsignedTxDTO(
            to=to_checksum_address(self.binary_module),
            data=encode_finalize_market(market_id),
            value=0,
            chain_id=self.chain_id,
            description=f"finalize market {market_id}",
            metadata={"gas": hex(10_000_000)},
        )

    def prepare_poke_oracle(self, oracle_question_id: int) -> UnsignedTxDTO:
        return UnsignedTxDTO(
            to=to_checksum_address(self.binary_module),
            data=encode_poke_oracle(int(oracle_question_id)),
            value=0,
            chain_id=self.chain_id,
            description=f"poke oracle {oracle_question_id}",
            metadata={"gas": hex(10_000_000)},
        )

    def prepare_sync_settlement(self, market_id: str) -> UnsignedTxDTO:
        return UnsignedTxDTO(
            to=to_checksum_address(self.binary_module),
            data=encode_sync_settlement(market_id),
            value=0,
            chain_id=self.chain_id,
            description=f"sync settlement {market_id}",
            metadata={"gas": hex(10_000_000)},
        )

    # -------------------------------------------------------------- internals

    def _opening_prices_for_markets(self, market_ids: list[str]) -> dict[str, Decimal]:
        """Map marketId → opening USD price via MarketReferenceLink + OracleAnswer."""
        ids = [mid for mid in market_ids if mid]
        if not ids:
            return {}

        link_data = self._graphql(
            """
            query($ids: [String!]!) {
              MarketReferenceLink(where: {market_id: {_in: $ids}}) {
                market_id
                referenceQuestionId
                pending
              }
            }
            """,
            {"ids": ids},
        )
        links = link_data.get("MarketReferenceLink") or []
        ref_by_market: dict[str, int] = {}
        ref_ids: list[int] = []
        for link in links:
            if link.get("pending"):
                continue
            market_id = str(link.get("market_id") or "")
            try:
                ref_id = int(link.get("referenceQuestionId"))
            except (TypeError, ValueError):
                continue
            if not market_id:
                continue
            ref_by_market[market_id] = ref_id
            ref_ids.append(ref_id)

        if not ref_ids:
            return {}

        answer_data = self._graphql(
            """
            query($ids: [numeric!]!) {
              OracleAnswer(where: {oracleQuestionId: {_in: $ids}}) {
                oracleQuestionId
                numericValue
                outcomeLabel
              }
            }
            """,
            {"ids": list(dict.fromkeys(ref_ids))},
        )
        price_by_ref: dict[int, Decimal] = {}
        for answer in answer_data.get("OracleAnswer") or []:
            try:
                ref_id = int(answer.get("oracleQuestionId"))
            except (TypeError, ValueError):
                continue
            parsed = _parse_opening_price(answer)
            if parsed is not None:
                price_by_ref[ref_id] = parsed

        return {
            market_id: price_by_ref[ref_id]
            for market_id, ref_id in ref_by_market.items()
            if ref_id in price_by_ref
        }

    def _row_to_event(
        self,
        row: dict[str, Any],
        *,
        opening_price: Decimal | None = None,
    ) -> EventDTO:
        decimals = int(row.get("quoteDecimals") or self.decimals)
        yes_price = _human_price(row.get("lastPrice"), decimals)
        no_price = (Decimal("1") - yes_price).quantize(Decimal("0.000001"))
        market_id = str(row.get("marketId") or row.get("id") or "")
        asset = str(row.get("asset") or "UNKNOWN")
        pool = str(row.get("poolAddress") or row.get("binaryPoolAddress") or "") or None
        status = str(row.get("clobStatus") or "Trading")
        if row.get("voided"):
            status = "Voided"
        winning_side = _winning_side(row.get("winningOutcome"))

        yes_symbol = _outcome_symbol(asset, market_id, "YES")
        no_symbol = _outcome_symbol(asset, market_id, "NO")

        # Prefer oracle opening price; if Market.strike is set, treat as USD cents.
        resolved_opening = opening_price
        strike_raw = int(_dec(row.get("strike")))
        if resolved_opening is None and strike_raw > 0:
            resolved_opening = (Decimal(strike_raw) / Decimal("100")).quantize(Decimal("0.01"))

        return EventDTO(
            market_id=market_id,
            asset=asset,
            strike=strike_raw,
            interval_sec=int(_dec(row.get("intervalSec"), "900")),
            expiry=_ts(row.get("expiry")),
            trading_start=_ts(row.get("tradingStart")),
            yes_token_id=str(row.get("yesTokenId") or ""),
            no_token_id=str(row.get("noTokenId") or ""),
            yes_price=yes_price,
            no_price=no_price,
            cumulative_quote_volume=_human_amount(row.get("cumulativeQuoteVolume"), decimals),
            last_price=yes_price,
            venue_id=str(row.get("venueId") or ""),
            status=status,
            pool_address=pool,
            market_address=str(row.get("marketAddress") or "") or None,
            collateral=str(row.get("collateral") or "") or None,
            trade_count=int(_dec(row.get("tradeCount"))),
            oracle_question_id=str(row.get("oracleQuestionId") or "") or None,
            cumulative_base_volume=_human_amount(row.get("cumulativeBaseVolume"), decimals),
            yes_symbol=yes_symbol,
            no_symbol=no_symbol,
            winning_outcome=winning_side,
            question=str(row.get("question") or ""),
            opening_price=resolved_opening,
            operator_id=int(_dec(row.get("operatorId"))),
        )

    def _row_to_fill(self, row: dict[str, Any]) -> FillDTO:
        return FillDTO(
            id=str(row.get("id") or ""),
            market_id=str(row.get("market_id") or ""),
            pool=str(row.get("pool") or ""),
            fill_price=_human_price(row.get("fillPrice"), self.decimals),
            quantity=_human_amount(row.get("quantity"), self.decimals),
            quote_quantity=_human_amount(row.get("quoteQuantity"), self.decimals),
            maker=str(row.get("maker") or ""),
            taker=str(row.get("taker") or ""),
            maker_side=_map_side(row.get("makerSide")),
            taker_side=_map_side(row.get("takerSide")),
            maker_is_buy=_is_buy_side(row.get("makerSide")),
            taker_is_buy=_is_buy_side(row.get("takerSide")),
            kind=str(row.get("kind") or ""),
            taker_is_bid=bool(row.get("takerIsBid")),
            taker_order="",
            timestamp=_ts(row.get("timestamp")),
            tx_hash=str(row.get("txHash") or ""),
            trader_label=None,
        )

    def _market_id_from_symbol(self, yes_symbol: str) -> str:
        # Symbols we mint look like BTC-<short>/USDso#YES — resolve via asset scan if needed.
        if yes_symbol.startswith("0x") and len(yes_symbol) >= 66:
            return yes_symbol
        # Prefer looking up by yes_symbol stored in DB-backed sync; for live adapter,
        # parse short id suffix when present.
        if "-" in yes_symbol and "/" in yes_symbol:
            mid = yes_symbol.split("-", 1)[1].split("/", 1)[0]
            # Find market ending with this suffix
            query = f"""
            query($limit: Int!) {{
              Market(
                where: {{marketType: {{_eq: BINARY}}, clobStatus: {{_eq: "Trading"}}}}
                limit: $limit
                order_by: {{expiry: asc}}
              ) {{
                {_MARKET_SELECTION}
              }}
            }}
            """
            data = self._graphql(query, {"limit": 100})
            for row in data.get("Market") or []:
                market_id = str(row.get("marketId") or "")
                if market_id.endswith(mid):
                    return market_id
        raise DreamDEXNotFound(f"Cannot resolve market for symbol {yes_symbol}")

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._http.post(
                self.indexer_url,
                json={"query": query, "variables": variables or {}},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.exception("DreamDEX indexer HTTP failure")
            raise DreamDEXUnavailable(f"Indexer unreachable: {exc}") from exc

        payload = response.json()
        if payload.get("errors"):
            logger.error("GraphQL errors: %s", payload["errors"])
            raise DreamDEXUnavailable(f"Indexer GraphQL error: {payload['errors']}")
        return payload.get("data") or {}

    def close(self) -> None:
        self._http.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
