"""Trade preparation and confirmation against DreamDEX."""

from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.events.models import EventContract, EventOutcome
from apps.trading.models import Trade
from integrations.dreamdex.adapter import get_adapter
from integrations.dreamdex.exceptions import (
    DreamDEXNotFound,
    DreamDEXUnavailable,
    DreamDEXValidationError,
)
from integrations.dreamdex.types import TradeIntent, UnsignedTxDTO

logger = logging.getLogger("dreamlens.services.trading")

ACTIVE_STATUSES = {
    EventContract.Status.TRADING,
    EventContract.Status.LIVE,
    EventContract.Status.LISTED,
}


class TradingError(Exception):
    pass


def _resolve_event_and_outcome(
    event_id: int,
    outcome_type: str,
) -> tuple[EventContract, EventOutcome]:
    try:
        event = EventContract.objects.prefetch_related("outcomes").get(pk=event_id)
    except EventContract.DoesNotExist as exc:
        raise TradingError(f"Event {event_id} not found") from exc

    if event.status not in ACTIVE_STATUSES:
        raise TradingError(f"Event {event_id} is not tradable")

    if event.expiry_time <= timezone.now():
        raise TradingError(f"Event {event_id} has expired")

    outcome = event.outcomes.filter(outcome_type=outcome_type.upper()).first()
    if not outcome:
        raise TradingError(f"Outcome {outcome_type} not found for event {event_id}")

    return event, outcome


def _side_for_outcome(outcome_type: str) -> str:
    side = outcome_type.upper()
    if side == "YES":
        return "BUY_YES"
    if side == "NO":
        return "BUY_NO"
    raise TradingError(f"Invalid outcome type: {outcome_type}")


_FEE_KEYS = ("gas", "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas")
_MIN_TRADE_HEADROOM_SEC = 90


def _tx_payload(unsigned: UnsignedTxDTO) -> dict:
    payload = {
        "to": unsigned.to,
        "data": unsigned.data,
        "value": str(unsigned.value),
        "chain_id": unsigned.chain_id,
        "chainId": hex(int(unsigned.chain_id)),
        "description": unsigned.description,
        "metadata": unsigned.metadata,
    }
    meta = unsigned.metadata or {}
    for key in _FEE_KEYS:
        if meta.get(key):
            payload[key] = meta[key]
    return payload


def _notional_to_quantity(notional: Decimal, price: Decimal) -> Decimal:
    if price <= 0:
        raise TradingError("Market price is unavailable")
    quantity = (notional / price).quantize(Decimal("0.000001"))
    if quantity <= 0:
        raise TradingError("Amount is too small for this market")
    return quantity


def _is_allowance_error(exc: BaseException) -> bool:
    msg = str(exc).lower().replace("0x", "")
    return "approved" in msg or "allowance" in msg or "fb8f41b2" in msg


def _is_unfillable_taker(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        hint in msg
        for hint in (
            "no liquidity",
            "cannot fill the full size",
            "try a smaller amount",
        )
    )


def _ensure_market_is_trading(adapter, event: EventContract) -> None:
    get_onchain = getattr(adapter, "get_market_onchain", None)
    if not callable(get_onchain):
        return
    try:
        onchain = get_onchain(event.external_id)
    except DreamDEXNotFound:
        raise
    except Exception:
        logger.warning("on-chain market status check failed event=%s", event.pk, exc_info=True)
        return
    label = (onchain.status_label or "").lower()
    if int(getattr(onchain, "status", 0) or 0) != 1 and label not in {"trading", "live"}:
        raise TradingError("This market is no longer trading. Pick a later window on Explore.")


def _fallback_to_limit_or_raise(adapter, intent: TradeIntent, exc: DreamDEXValidationError) -> UnsignedTxDTO:
    if intent.order_type != "MARKET" or not _is_unfillable_taker(exc):
        raise TradingError(str(exc)) from exc
    try:
        unsigned = adapter.prepare_place_order(replace(intent, order_type="LIMIT"))
    except (DreamDEXUnavailable, DreamDEXValidationError) as inner:
        raise TradingError(str(inner)) from inner
    logger.info(
        "MARKET/IOC not fillable; resting LIMIT at displayed price market=%s",
        intent.market_id,
    )
    return unsigned


def _quote_wallet_fees(adapter, unsigned: UnsignedTxDTO | None, wallet_address: str, *, estimate: bool) -> None:
    if unsigned is None:
        return
    quote = getattr(adapter, "quote_wallet_fees", None)
    if not callable(quote):
        return
    quote(unsigned, account=wallet_address, estimate=estimate)


@transaction.atomic
def prepare_trade(
    user,
    event_id: int,
    outcome: str,
    amount: Decimal,
    wallet_address: str,
    *,
    amount_is_notional: bool = False,
) -> tuple[Trade, UnsignedTxDTO, UnsignedTxDTO | None]:
    """Create PREPARED trade and unsigned DreamDEX txs for the wallet to sign."""
    if not wallet_address:
        raise TradingError("Wallet address required")

    if amount <= 0:
        raise TradingError("Amount must be positive")

    event, outcome_obj = _resolve_event_and_outcome(event_id, outcome)
    if not event.pool_address:
        raise TradingError("Market has no DreamDEX pool address")

    price = outcome_obj.current_price
    quantity = _notional_to_quantity(amount, price) if amount_is_notional else amount

    now_ts = int(timezone.now().timestamp())
    expiry_ts = int(event.expiry_time.timestamp())
    if expiry_ts - now_ts < _MIN_TRADE_HEADROOM_SEC:
        raise TradingError("This market is about to lock. Pick a later window.")
    expire_sec = min(now_ts + 300, expiry_ts - 1)

    adapter = get_adapter()
    _ensure_market_is_trading(adapter, event)
    side = _side_for_outcome(outcome_obj.outcome_type)
    intent = TradeIntent(
        market_id=event.external_id,
        pool=event.pool_address,
        side=side,
        price=price,
        quantity=quantity,
        order_type="MARKET",
        account=wallet_address,
        expire_timestamp_ns=expire_sec * 1_000_000_000,
    )
    try:
        unsigned = adapter.prepare_place_order(intent)
    except DreamDEXNotFound:
        raise
    except (DreamDEXUnavailable, DreamDEXValidationError) as exc:
        raise TradingError(str(exc)) from exc

    qty_raw_meta = unsigned.metadata.get("quantity_raw") if unsigned.metadata else None
    if qty_raw_meta:
        decimals = int(getattr(settings, "DREAMDEX_COLLATERAL_DECIMALS", 6) or 6)
        snapped = Decimal(qty_raw_meta) / (Decimal(10) ** decimals)
        if snapped > 0:
            quantity = snapped.quantize(Decimal("0.000001"))

    approval = None
    prepare_approval = getattr(adapter, "prepare_collateral_approval", None)
    if callable(prepare_approval):
        decimals = int(getattr(settings, "DREAMDEX_COLLATERAL_DECIMALS", 6) or 6)
        scale = Decimal(10) ** decimals
        price_raw_meta = unsigned.metadata.get("price_raw") if unsigned.metadata else None
        qty_raw_meta = unsigned.metadata.get("quantity_raw") if unsigned.metadata else None
        if price_raw_meta and qty_raw_meta:
            qty_raw = int(qty_raw_meta)
            yes_raw = int(price_raw_meta)
            pay_raw = yes_raw if side in {"BUY_YES", "SELL_NO"} else (int(scale) - yes_raw)
            quote_raw = (qty_raw * max(pay_raw, 1)) // int(scale)
        else:
            quote_raw = int((quantity * price * scale).to_integral_value())
        try:
            approval = prepare_approval(
                account=wallet_address,
                spender=event.pool_address,
                amount_raw=max(quote_raw, 1),
                collateral=event.collateral or None,
            )
        except Exception:
            logger.exception("collateral approval check failed")

    # Always estimate the order. Allowance-only reverts are expected when an
    # approval tx still has to mine; every other revert must fail closed here
    # instead of asking MetaMask to sign a transaction that will fail.
    try:
        _quote_wallet_fees(adapter, unsigned, wallet_address, estimate=True)
    except DreamDEXValidationError as exc:
        if approval is not None and _is_allowance_error(exc):
            _quote_wallet_fees(adapter, unsigned, wallet_address, estimate=False)
        else:
            unsigned = _fallback_to_limit_or_raise(adapter, intent, exc)
            try:
                _quote_wallet_fees(adapter, unsigned, wallet_address, estimate=approval is None)
            except DreamDEXValidationError as inner:
                if approval is not None and _is_allowance_error(inner):
                    _quote_wallet_fees(adapter, unsigned, wallet_address, estimate=False)
                else:
                    raise TradingError(str(inner)) from inner
            except Exception:
                logger.exception("wallet fee quote failed after LIMIT fallback")
    except Exception:
        logger.exception("wallet fee quote failed")
    if approval is None:
        simulate = getattr(adapter, "simulate_unsigned_tx", None)
        if callable(simulate) and not getattr(adapter, "quote_wallet_fees", None):
            try:
                simulate(unsigned, account=wallet_address)
            except DreamDEXValidationError as exc:
                unsigned = _fallback_to_limit_or_raise(adapter, intent, exc)
                try:
                    simulate(unsigned, account=wallet_address)
                except DreamDEXValidationError as inner:
                    raise TradingError(str(inner)) from inner
    try:
        _quote_wallet_fees(adapter, approval, wallet_address, estimate=True)
    except DreamDEXValidationError as exc:
        raise TradingError(str(exc)) from exc
    except Exception:
        logger.exception("approval fee quote failed")

    trade = Trade.objects.create(
        user=user,
        event=event,
        outcome=outcome_obj,
        side=Trade.Side.BUY,
        amount=quantity,
        entry_price=price,
        status=Trade.Status.PREPARED,
        metadata_json={
            "wallet_address": wallet_address,
            "notional_usd": str(amount if amount_is_notional else quantity * price),
            "unsigned_tx": _tx_payload(unsigned),
            "approval_tx": _tx_payload(approval) if approval else None,
        },
    )
    trade.status = Trade.Status.AWAITING_CONFIRMATION
    trade.save(update_fields=["status"])

    logger.info("prepare_trade trade_id=%s event=%s", trade.pk, event.pk)
    return trade, unsigned, approval


@transaction.atomic
def confirm_trade(trade_id: int, tx_hash: str, *, user=None) -> Trade:
    """Advance trade SUBMITTED → CONFIRMED after an on-chain receipt (live)."""
    try:
        qs = Trade.objects.select_for_update().select_related("event", "outcome")
        if user is not None:
            qs = qs.filter(user=user)
        trade = qs.get(pk=trade_id)
    except Trade.DoesNotExist as exc:
        raise TradingError(f"Trade {trade_id} not found") from exc

    if trade.status not in (
        Trade.Status.PREPARED,
        Trade.Status.AWAITING_CONFIRMATION,
        Trade.Status.SUBMITTED,
    ):
        raise TradingError(f"Trade {trade_id} cannot be confirmed (status={trade.status})")

    if not tx_hash:
        raise TradingError("Transaction hash required")

    _resolve_event_and_outcome(trade.event_id, trade.outcome.outcome_type)

    trade.transaction_hash = tx_hash
    trade.status = Trade.Status.SUBMITTED
    trade.save(update_fields=["transaction_hash", "status"])

    if not getattr(settings, "MOCK_DREAMDEX", False):
        from integrations.metamask.transactions import wait_for_receipt

        try:
            receipt = wait_for_receipt(tx_hash, timeout=90)
        except Exception as exc:  # noqa: BLE001
            logger.warning("confirm_trade receipt wait failed trade_id=%s: %s", trade.pk, exc)
            raise TradingError(f"Transaction not confirmed on Somnia yet: {exc}") from exc
        status = int(receipt.get("status", 0) if isinstance(receipt, dict) else receipt.status)
        if status != 1:
            trade.status = Trade.Status.FAILED
            trade.save(update_fields=["status"])
            reason = "On-chain transaction failed"
            try:
                from integrations.dreamdex.client import explain_reverted_tx

                reason = explain_reverted_tx(tx_hash)
            except Exception:
                logger.exception("could not decode reverted trade_id=%s", trade.pk)
            raise TradingError(reason)

    trade.status = Trade.Status.CONFIRMED
    trade.settled_at = timezone.now()
    trade.save(update_fields=["status", "settled_at"])

    from services.portfolio_service import sync_positions

    try:
        sync_positions(trade.user)
    except Exception:
        logger.exception("sync_positions failed after confirm trade_id=%s", trade.pk)

    logger.info("confirm_trade trade_id=%s tx=%s", trade.pk, tx_hash)
    return trade


def expire_stale_trades(*, max_age_minutes: int = 30) -> int:
    """Mark old AWAITING_CONFIRMATION trades as EXPIRED."""
    cutoff = timezone.now() - timezone.timedelta(minutes=max_age_minutes)
    updated = Trade.objects.filter(
        status=Trade.Status.AWAITING_CONFIRMATION,
        opened_at__lt=cutoff,
    ).update(status=Trade.Status.EXPIRED)
    return updated
