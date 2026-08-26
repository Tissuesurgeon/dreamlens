"""Portfolio sync and summary from confirmed trades and on-chain fills."""

from __future__ import annotations

import logging
from decimal import Decimal

from django.core.cache import cache
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.events.models import EventContract
from apps.portfolio.models import Position
from apps.trading.models import Trade

logger = logging.getLogger("dreamlens.services.portfolio")

FOUR_PLACES = Decimal("0.0001")
_FILLS_CACHE_TTL = 20


def _usable_market_id(value: str | None) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw.startswith("0x"):
        return raw or None
    if set(raw[2:]) <= {"0"}:
        return None
    return raw


def _user_fill_accounts(user) -> list[str]:
    """EOA wallets plus the user's Smart Account, de-duplicated."""
    seen: set[str] = set()
    accounts: list[str] = []
    for wallet in user.wallets.all():
        addr = (wallet.address or "").strip()
        key = addr.lower()
        if addr.startswith("0x") and key not in seen:
            seen.add(key)
            accounts.append(addr)
    try:
        from apps.agents.models import SmartAccount
    except Exception:
        return accounts
    for sa in SmartAccount.objects.filter(user=user).exclude(address=""):
        addr = (sa.address or "").strip()
        key = addr.lower()
        if addr.startswith("0x") and key not in seen:
            seen.add(key)
            accounts.append(addr)
    return accounts


def _event_maps() -> tuple[dict[str, EventContract], dict[str, EventContract]]:
    events = EventContract.objects.prefetch_related("outcomes").exclude(pool_address="")
    by_pool: dict[str, EventContract] = {}
    by_market: dict[str, EventContract] = {}
    for event in events:
        pool = (event.pool_address or "").lower()
        if pool:
            by_pool[pool] = event
        for candidate in (
            event.external_id,
            (event.metadata_json or {}).get("market_id"),
        ):
            mid = _usable_market_id(candidate if isinstance(candidate, str) else None)
            if mid:
                by_market[mid] = event
    return by_pool, by_market


def _event_for_fill(fill, by_pool: dict, by_market: dict) -> EventContract | None:
    pool = (getattr(fill, "pool", None) or "").lower()
    if pool and pool in by_pool:
        return by_pool[pool]
    mid = _usable_market_id(getattr(fill, "market_id", None))
    if mid and mid in by_market:
        return by_market[mid]
    return None


def _fill_side_for_wallet(fill, wallet: str) -> str | None:
    w = wallet.lower()
    if fill.taker and fill.taker.lower() == w:
        return fill.taker_side
    if fill.maker and fill.maker.lower() == w:
        return fill.maker_side
    return None


def _outcome_for_side(event: EventContract, side: str | None):
    want = (side or "").upper()
    if want not in ("YES", "NO"):
        return None
    for outcome in event.outcomes.all():
        if outcome.outcome_type == want:
            return outcome
    return None


def sync_trades_from_fills(user, *, force: bool = False) -> dict[str, int]:
    """Import DreamDEX fills for the user's EOA and Smart Account into Trade rows."""
    accounts = _user_fill_accounts(user)
    if not accounts:
        return {"imported": 0, "skipped": 0}

    cache_key = f"portfolio:fills:{user.pk}"
    if not force:
        try:
            has_trades = Trade.objects.filter(
                user=user,
                status=Trade.Status.CONFIRMED,
            ).exists()
            if cache.get(cache_key) and has_trades:
                return {"imported": 0, "skipped": 0}
        except Exception:
            logger.warning("fill sync cache unavailable")

    from integrations.dreamdex.adapter import get_adapter

    try:
        adapter = get_adapter()
    except Exception:
        logger.exception("adapter unavailable for fill sync user=%s", user.pk)
        return {"imported": 0, "skipped": 0}

    fills_found: list[tuple[str, object]] = []
    fetch_ok = True
    for account in accounts:
        try:
            for fill in adapter.get_user_fills(account):
                fills_found.append((account.lower(), fill))
        except Exception:
            fetch_ok = False
            logger.exception("get_user_fills failed account=%s", account)

    if not fills_found:
        if fetch_ok:
            try:
                cache.set(cache_key, "1", timeout=_FILLS_CACHE_TTL)
            except Exception:
                logger.warning("Failed to write fill sync cache")
        return {"imported": 0, "skipped": 0}

    by_pool, by_market = _event_maps()
    existing_ext = set(
        Trade.objects.filter(user=user)
        .exclude(external_trade_id="")
        .values_list("external_trade_id", flat=True)
    )
    ui_tx = {
        h.lower()
        for h in Trade.objects.filter(
            user=user,
            status=Trade.Status.CONFIRMED,
            external_trade_id="",
        )
        .exclude(transaction_hash="")
        .values_list("transaction_hash", flat=True)
    }

    imported = 0
    skipped = 0
    seen_ext = set(existing_ext)

    for account, fill in fills_found:
        ext_id = f"{fill.id}:{account}"
        if ext_id in seen_ext:
            skipped += 1
            continue
        tx = (getattr(fill, "tx_hash", None) or "").lower()
        if tx and tx in ui_tx:
            skipped += 1
            continue
        event = _event_for_fill(fill, by_pool, by_market)
        if event is None:
            skipped += 1
            logger.info(
                "fill skipped no matching event fill=%s pool=%s",
                getattr(fill, "id", ""),
                getattr(fill, "pool", ""),
            )
            continue
        outcome = _outcome_for_side(event, _fill_side_for_wallet(fill, account))
        if outcome is None:
            skipped += 1
            continue
        amount = getattr(fill, "quantity", None) or Decimal("0")
        if amount <= 0:
            skipped += 1
            continue
        opened_at = fill.timestamp
        if timezone.is_naive(opened_at):
            opened_at = timezone.make_aware(opened_at, timezone.utc)

        trade = Trade.objects.create(
            user=user,
            event=event,
            outcome=outcome,
            side=Trade.Side.BUY,
            amount=amount,
            entry_price=fill.fill_price,
            external_trade_id=ext_id,
            transaction_hash=fill.tx_hash or "",
            status=Trade.Status.CONFIRMED,
            metadata_json={
                "source": "dreamdex_fill",
                "wallet": account,
                "fill_id": fill.id,
                "quote_quantity": str(fill.quote_quantity),
            },
        )
        Trade.objects.filter(pk=trade.pk).update(
            opened_at=opened_at,
            settled_at=opened_at,
        )
        seen_ext.add(ext_id)
        imported += 1

    if fetch_ok:
        try:
            cache.set(cache_key, "1", timeout=_FILLS_CACHE_TTL)
        except Exception:
            logger.warning("Failed to write fill sync cache")

    logger.info(
        "sync_trades_from_fills user=%s imported=%s skipped=%s",
        user.pk,
        imported,
        skipped,
    )
    return {"imported": imported, "skipped": skipped}


def refresh_portfolio(user) -> dict[str, int]:
    """Pull on-chain fills, refresh market outcomes, then rebuild positions."""
    fill_stats = {"imported": 0, "skipped": 0}
    try:
        fill_stats = sync_trades_from_fills(user)
    except Exception:
        logger.exception("sync_trades_from_fills failed user=%s", user.pk)
    try:
        from services.event_service import refresh_user_position_events

        refresh_user_position_events(user)
    except Exception:
        logger.exception("refresh_user_position_events failed user=%s", user.pk)
    pos_stats = sync_positions(user)
    return {**fill_stats, **pos_stats}


@transaction.atomic
def sync_positions(user) -> dict[str, int]:
    """Upsert positions from confirmed trades."""
    trades = Trade.objects.filter(
        user=user,
        status=Trade.Status.CONFIRMED,
    ).select_related("event", "outcome")

    created = 0
    updated = 0

    grouped: dict[tuple[int, int], list[Trade]] = {}
    for trade in trades:
        key = (trade.event_id, trade.outcome_id)
        grouped.setdefault(key, []).append(trade)

    for (event_id, outcome_id), group in grouped.items():
        total_amount = sum(t.amount for t in group)
        weighted_price = sum(t.amount * t.entry_price for t in group) / total_amount
        latest = max(group, key=lambda t: t.opened_at)
        earliest = min(t.opened_at for t in group)

        outcome = latest.outcome
        current_value = total_amount * outcome.current_price
        existing = Position.objects.filter(
            user=user,
            event_id=event_id,
            outcome_id=outcome_id,
        ).first()
        was_closed = bool(existing and existing.status == Position.Status.CLOSED)

        position, was_created = Position.objects.update_or_create(
            user=user,
            event_id=event_id,
            outcome_id=outcome_id,
            defaults={
                "amount": total_amount,
                "entry_price": weighted_price.quantize(FOUR_PLACES),
                "current_value": current_value.quantize(FOUR_PLACES),
                "status": Position.Status.OPEN,
                "pnl": (current_value - total_amount * weighted_price).quantize(FOUR_PLACES),
            },
        )
        Position.objects.filter(pk=position.pk).update(opened_at=earliest)
        position.opened_at = earliest
        if was_created:
            created += 1
        else:
            updated += 1

        event = EventContract.objects.get(pk=event_id)
        payout = settlement_payout_price(event, outcome)
        if payout is not None:
            cost = total_amount * weighted_price
            settled_value = (total_amount * payout).quantize(FOUR_PLACES)
            position.status = (
                Position.Status.CLOSED if was_closed else Position.Status.SETTLED
            )
            if position.settled_at is None:
                position.settled_at = timezone.now()
            position.current_value = settled_value
            position.pnl = (settled_value - cost).quantize(FOUR_PLACES)
            position.save()

    logger.info("sync_positions user=%s created=%s updated=%s", user.pk, created, updated)
    return {"created": created, "updated": updated}


def get_portfolio_summary(user) -> dict:
    """Aggregate portfolio metrics for a user."""
    refresh_portfolio(user)

    positions = Position.objects.filter(user=user)
    open_positions = positions.filter(status=Position.Status.OPEN).count()
    completed = positions.filter(
        status__in=[Position.Status.CLOSED, Position.Status.SETTLED]
    ).count()

    total_pnl = positions.aggregate(total=Sum("pnl"))["total"] or Decimal("0")

    trades = Trade.objects.filter(user=user, status=Trade.Status.CONFIRMED)
    trade_count = trades.count()
    wins = trades.filter(pnl__gt=0).count() if trades.filter(pnl__isnull=False).exists() else 0
    win_rate = Decimal(wins / trade_count) if trade_count else Decimal("0")

    return {
        "total_pnl": total_pnl.quantize(FOUR_PLACES),
        "open_positions": open_positions,
        "completed_positions": completed,
        "trade_count": trade_count,
        "win_rate": win_rate.quantize(FOUR_PLACES),
        "disclaimer": "PnL reflects indexed trades — verify on-chain balances.",
    }


def list_positions(user, *, status: str | None = None) -> list[Position]:
    refresh_portfolio(user)
    qs = Position.objects.filter(user=user).select_related("event", "outcome")
    if status:
        qs = qs.filter(status=status.upper())
    return list(qs.order_by("-opened_at"))


def list_recent_trades(user, *, limit: int = 50) -> list[Trade]:
    return list(
        Trade.objects.filter(user=user, status=Trade.Status.CONFIRMED)
        .select_related("event", "outcome")
        .order_by("-opened_at")[: max(int(limit), 0)]
    )


class PortfolioError(Exception):
    pass


def settlement_payout_price(event, outcome) -> Decimal | None:
    """Collateral per share after resolution. None if the oracle has not posted yet."""
    if event.status == EventContract.Status.VOIDED:
        return Decimal("0.5")
    if event.status in (
        EventContract.Status.RESOLVED,
        EventContract.Status.FINALIZED,
    ):
        winner = (event.winning_outcome or "").strip().upper()
        if not winner:
            return None
        return Decimal("1") if outcome.outcome_type == winner else Decimal("0")
    return None


def position_result(position) -> str:
    event = position.event
    payout = settlement_payout_price(event, position.outcome)
    if payout is not None:
        if event.status == EventContract.Status.VOIDED:
            return "void"
        if payout > 0:
            return "claimed" if position.status == Position.Status.CLOSED else "won"
        return "lost"
    if event.status in (
        EventContract.Status.LOCKED,
        EventContract.Status.SETTLING,
    ) or event.expiry_time <= timezone.now():
        return "settling"
    return "open"


def _position_wallet(user, position) -> str:
    trade = (
        Trade.objects.filter(
            user=user,
            event=position.event,
            outcome=position.outcome,
            status=Trade.Status.CONFIRMED,
        )
        .order_by("-opened_at")
        .first()
    )
    if trade:
        wallet = str((trade.metadata_json or {}).get("wallet") or "").strip()
        if wallet:
            return wallet
    row = user.wallets.filter(is_primary=True).first() or user.wallets.first()
    return (row.address if row else "") or ""


def _outcome_held(balances, outcome_type: str) -> Decimal:
    key = "yes_balance" if outcome_type.upper() == "YES" else "no_balance"
    if isinstance(balances, dict):
        return Decimal(str(balances.get(key) or 0))
    return Decimal(str(getattr(balances, key, 0) or 0))


def annotate_positions(user, positions: list[Position]) -> list[Position]:
    """Attach result / claimable flags used by the portfolio page."""
    from integrations.dreamdex.adapter import get_adapter

    adapter = None
    for position in positions:
        position.result = position_result(position)
        position.claimable = False
        position.claim_wallet = ""
        position.claim_payout = None
        if position.result not in ("won", "void"):
            continue
        wallet = _position_wallet(user, position)
        position.claim_wallet = wallet
        held = position.amount
        if wallet and position.event.external_id:
            try:
                if adapter is None:
                    adapter = get_adapter()
                held = _outcome_held(
                    adapter.get_outcome_balances(wallet, position.event.external_id),
                    position.outcome.outcome_type,
                )
            except Exception:
                logger.warning(
                    "outcome balance read failed position=%s",
                    position.pk,
                    exc_info=True,
                )
        if held > 0:
            position.claimable = True
            payout = settlement_payout_price(position.event, position.outcome) or Decimal("0")
            position.claim_payout = (held * payout).quantize(FOUR_PLACES)
    return positions


def _to_raw_amount(human: Decimal) -> int:
    from django.conf import settings

    scale = Decimal(10) ** int(settings.DREAMDEX_COLLATERAL_DECIMALS)
    return int((human * scale).to_integral_value())


def prepare_position_redeem(user, position_id: int, wallet_address: str):
    """Build the user-signed BinaryMarketsModule.redeem tx for a winning or voided position."""
    from django.conf import settings

    from integrations.dreamdex.adapter import get_adapter
    from services.trading_service import _quote_wallet_fees, _tx_payload

    if not wallet_address:
        raise PortfolioError("Wallet address required")
    try:
        position = Position.objects.select_related("event", "outcome").get(
            pk=position_id,
            user=user,
        )
    except Position.DoesNotExist as exc:
        raise PortfolioError("Position not found") from exc

    result = position_result(position)
    if result not in ("won", "void"):
        raise PortfolioError("This position has nothing to claim yet.")

    expected = _position_wallet(user, position).lower()
    if expected and expected != wallet_address.lower():
        raise PortfolioError(
            "Switch MetaMask to the wallet that holds these outcome tokens."
        )

    adapter = get_adapter()
    held = position.amount
    try:
        held = _outcome_held(
            adapter.get_outcome_balances(wallet_address, position.event.external_id),
            position.outcome.outcome_type,
        )
    except Exception:
        logger.warning("outcome balance read failed for redeem position=%s", position.pk)
    if held <= 0:
        raise PortfolioError("Nothing left to claim on-chain for this position.")

    amount_raw = _to_raw_amount(held)
    if amount_raw <= 0:
        raise PortfolioError("Claim size is below the venue lot size.")

    outcome_idx = 0 if position.outcome.outcome_type == "YES" else 1
    approval = None
    prep_op = getattr(adapter, "prepare_outcome_operator_approval", None)
    if callable(prep_op):
        try:
            approval = prep_op(account=wallet_address)
        except Exception as exc:
            logger.warning("outcome operator approval check failed: %s", exc)

    try:
        unsigned = adapter.prepare_redeem(
            market_id=position.event.external_id,
            account=wallet_address,
            outcome_idx=outcome_idx,
            amount=amount_raw,
        )
    except Exception as exc:
        raise PortfolioError(str(exc)) from exc

    # Redeem often reverts in eth_estimateGas until setOperator is mined.
    # Don't fail prepare — MetaMask estimates again after the approval tx.
    try:
        _quote_wallet_fees(adapter, unsigned, wallet_address, estimate=True)
    except Exception:
        logger.warning(
            "redeem gas estimate failed position=%s; using default fees",
            position.pk,
            exc_info=True,
        )
        try:
            _quote_wallet_fees(adapter, unsigned, wallet_address, estimate=False)
        except Exception:
            logger.warning("redeem fee quote failed position=%s", position.pk)
    if approval is not None:
        try:
            _quote_wallet_fees(adapter, approval, wallet_address, estimate=True)
        except Exception:
            try:
                _quote_wallet_fees(adapter, approval, wallet_address, estimate=False)
            except Exception:
                logger.warning("approval fee quote failed position=%s", position.pk)

    payout = settlement_payout_price(position.event, position.outcome) or Decimal("0")
    return {
        "position_id": position.pk,
        "unsigned_tx": _tx_payload(unsigned),
        "approval_tx": _tx_payload(approval) if approval else None,
        "outcome_idx": outcome_idx,
        "amount": str(held),
        "payout": str((held * payout).quantize(FOUR_PLACES)),
        "collateral_symbol": "Test USDC" if int(settings.DREAMDEX_CHAIN_ID) == 50312 else "USDso",
        "wallet_address": wallet_address,
    }


def confirm_position_redeem(user, position_id: int, tx_hash: str) -> Position:
    """Record a confirmed redeem. Mock adapters subtract claimed outcome tokens."""
    if not tx_hash or not str(tx_hash).startswith("0x"):
        raise PortfolioError("Transaction hash required")
    try:
        position = Position.objects.select_related("event", "outcome").get(
            pk=position_id,
            user=user,
        )
    except Position.DoesNotExist as exc:
        raise PortfolioError("Position not found") from exc

    from integrations.dreamdex.adapter import get_adapter

    adapter = get_adapter()
    record = getattr(adapter, "record_redeem", None)
    if callable(record):
        wallet = _position_wallet(user, position)
        try:
            held = _outcome_held(
                adapter.get_outcome_balances(wallet, position.event.external_id),
                position.outcome.outcome_type,
            )
            record(
                account=wallet,
                market_id=position.event.external_id,
                outcome_idx=0 if position.outcome.outcome_type == "YES" else 1,
                amount_raw=_to_raw_amount(held),
            )
        except Exception:
            logger.warning("mock redeem record failed position=%s", position.pk)
    position.status = Position.Status.CLOSED
    position.save(update_fields=["status"])
    logger.info(
        "position redeem confirmed user=%s position=%s tx=%s",
        user.pk,
        position.pk,
        tx_hash,
    )
    return position


def get_wallet_balances_for_user(user) -> dict | None:
    """On-chain native + collateral balances for the user's primary wallet."""
    wallet = user.wallets.filter(is_primary=True).first() or user.wallets.first()
    if not wallet:
        return None
    try:
        from integrations.dreamdex.adapter import get_adapter

        bal = get_adapter().get_wallet_balances(wallet.address)
    except Exception:
        logger.exception("wallet balance fetch failed for %s", wallet.address)
        return {
            "address": wallet.address,
            "native_balance": None,
            "native_symbol": None,
            "collateral_balance": None,
            "collateral_symbol": None,
            "collateral_address": None,
            "chain_id": wallet.chain_id,
            "error": "Could not read on-chain balances.",
        }
    return {
        "address": bal.address,
        "native_balance": str(bal.native_balance),
        "native_symbol": bal.native_symbol,
        "collateral_balance": str(bal.collateral_balance),
        "collateral_symbol": bal.collateral_symbol,
        "collateral_address": bal.collateral_address,
        "chain_id": bal.chain_id,
        "error": None,
    }
