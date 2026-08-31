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


def _fill_is_buy_for_wallet(fill, wallet: str) -> bool:
    w = wallet.lower()
    if fill.taker and fill.taker.lower() == w:
        return bool(getattr(fill, "taker_is_buy", True))
    if fill.maker and fill.maker.lower() == w:
        return bool(getattr(fill, "maker_is_buy", True))
    return True


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
            side=Trade.Side.BUY if _fill_is_buy_for_wallet(fill, account) else Trade.Side.SELL,
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
        buys = [t for t in group if t.side != Trade.Side.SELL]
        sells = [t for t in group if t.side == Trade.Side.SELL]
        buy_qty = sum((t.amount for t in buys), Decimal("0"))
        sell_qty = sum((t.amount for t in sells), Decimal("0"))
        remaining = buy_qty - sell_qty
        buy_cost = sum((t.amount * t.entry_price for t in buys), Decimal("0"))
        sell_proceeds = sum((t.amount * t.entry_price for t in sells), Decimal("0"))
        avg_entry = (buy_cost / buy_qty).quantize(FOUR_PLACES) if buy_qty > 0 else Decimal("0")
        latest = max(group, key=lambda t: t.opened_at)
        earliest = min(t.opened_at for t in group)

        outcome = latest.outcome
        existing = Position.objects.filter(
            user=user,
            event_id=event_id,
            outcome_id=outcome_id,
        ).first()
        was_closed = bool(existing and existing.status == Position.Status.CLOSED)

        event = EventContract.objects.get(pk=event_id)
        payout = settlement_payout_price(event, outcome)

        if remaining <= 0:
            realized = (sell_proceeds - buy_cost).quantize(FOUR_PLACES)
            position, was_created = Position.objects.update_or_create(
                user=user,
                event_id=event_id,
                outcome_id=outcome_id,
                defaults={
                    "amount": buy_qty if buy_qty > 0 else sell_qty,
                    "entry_price": avg_entry,
                    "current_value": Decimal("0"),
                    "status": Position.Status.CLOSED,
                    "pnl": realized,
                    "settled_at": timezone.now() if not (existing and existing.settled_at) else existing.settled_at,
                },
            )
            Position.objects.filter(pk=position.pk).update(opened_at=earliest)
            if was_created:
                created += 1
            else:
                updated += 1
            continue

        mark = remaining * outcome.current_price
        avg_sell = (sell_proceeds / sell_qty) if sell_qty > 0 else Decimal("0")
        realized = (sell_qty * (avg_sell - avg_entry)).quantize(FOUR_PLACES) if sell_qty > 0 else Decimal("0")
        unrealized = (remaining * (outcome.current_price - avg_entry)).quantize(FOUR_PLACES)
        current_value = mark.quantize(FOUR_PLACES)

        position, was_created = Position.objects.update_or_create(
            user=user,
            event_id=event_id,
            outcome_id=outcome_id,
            defaults={
                "amount": remaining,
                "entry_price": avg_entry,
                "current_value": current_value,
                "status": Position.Status.OPEN,
                "pnl": (realized + unrealized).quantize(FOUR_PLACES),
            },
        )
        Position.objects.filter(pk=position.pk).update(opened_at=earliest)
        position.opened_at = earliest
        if was_created:
            created += 1
        else:
            updated += 1

        if payout is not None:
            cost = remaining * avg_entry
            settled_value = (remaining * payout).quantize(FOUR_PLACES)
            position.status = (
                Position.Status.CLOSED if was_closed else Position.Status.SETTLED
            )
            if position.settled_at is None:
                position.settled_at = timezone.now()
            position.current_value = settled_value
            position.pnl = (settled_value - cost + realized).quantize(FOUR_PLACES)
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


def _confirmed_net_qty(position) -> Decimal:
    buy = Decimal("0")
    sell = Decimal("0")
    for trade in Trade.objects.filter(
        user_id=position.user_id,
        event_id=position.event_id,
        outcome_id=position.outcome_id,
        status=Trade.Status.CONFIRMED,
    ).only("side", "amount"):
        if trade.side == Trade.Side.SELL:
            sell += trade.amount
        else:
            buy += trade.amount
    return buy - sell


def position_result(position) -> str:
    event = position.event
    payout = settlement_payout_price(event, position.outcome)
    if _confirmed_net_qty(position) <= 0 and position.status == Position.Status.CLOSED:
        has_sell = Trade.objects.filter(
            user_id=position.user_id,
            event_id=position.event_id,
            outcome_id=position.outcome_id,
            side=Trade.Side.SELL,
            status=Trade.Status.CONFIRMED,
        ).exists()
        if has_sell:
            return "closed"
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
    if position.status == Position.Status.CLOSED:
        return "closed"
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
        meta = trade.metadata_json or {}
        wallet = str(meta.get("wallet") or meta.get("wallet_address") or "").strip()
        if wallet:
            return wallet
    row = user.wallets.filter(is_primary=True).first() or user.wallets.first()
    return (row.address if row else "") or ""


def _position_is_agent_fill(user, position, sa) -> bool:
    """True when this fill was placed onto the Hybrid Smart Account."""
    if sa is None or not getattr(sa, "address", ""):
        return False
    sa_addr = sa.address.lower()
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
    if trade is None:
        return False
    meta = trade.metadata_json or {}
    src = str(meta.get("source") or "").lower()
    if src in {"telegram", "copy", "dream_agent"}:
        return True
    wallet = str(meta.get("wallet") or meta.get("wallet_address") or "").strip().lower()
    return bool(wallet and wallet == sa_addr)


def _smart_account_for_user(user):
    try:
        from apps.agents.models import SmartAccount
    except Exception:
        return None
    return (
        SmartAccount.objects.filter(user=user)
        .exclude(address="")
        .order_by("-updated_at")
        .first()
    )


def _is_smart_account(user, address: str) -> bool:
    sa = _smart_account_for_user(user)
    return bool(sa and address and sa.address.lower() == address.lower())


def _signer_for_holder(user, holder: str) -> str:
    """MetaMask account that must sign. Owner EOA when tokens sit on the Smart Account."""
    sa = _smart_account_for_user(user)
    if sa and holder and sa.address.lower() == holder.lower():
        return sa.owner_address or holder
    return holder


def _held_on(adapter, account: str, position) -> Decimal | None:
    """On-chain outcome balance, or None if the read failed."""
    if not account or not position.event.external_id:
        return Decimal("0")
    try:
        return _outcome_held(
            adapter.get_outcome_balances(account, position.event.external_id),
            position.outcome.outcome_type,
        )
    except Exception:
        logger.warning(
            "outcome balance read failed account=%s position=%s",
            account,
            position.pk,
            exc_info=True,
        )
        return None


def _resolve_claim(user, position, adapter) -> tuple[str, Decimal, bool, str]:
    """Return (token_holder, held, via_smart_account, metamask_signer)."""
    accounts = list(_user_fill_accounts(user))
    trade_wallet = _position_wallet(user, position)
    if trade_wallet and trade_wallet.lower() not in {a.lower() for a in accounts}:
        accounts.insert(0, trade_wallet)

    held_by: list[tuple[str, Decimal]] = []
    any_read = False
    for account in accounts:
        qty = _held_on(adapter, account, position)
        if qty is None:
            continue
        any_read = True
        if qty > 0:
            held_by.append((account, qty))

    if held_by:
        sa_hits = [(a, q) for a, q in held_by if _is_smart_account(user, a)]
        if sa_hits:
            account, qty = sa_hits[0]
            return account, qty, True, _signer_for_holder(user, account)
        for account, qty in held_by:
            if trade_wallet and account.lower() == trade_wallet.lower():
                return account, qty, False, _signer_for_holder(user, account)
        account, qty = held_by[0]
        return account, qty, _is_smart_account(user, account), _signer_for_holder(
            user, account
        )

    if not any_read and position.amount > 0:
        sa = _smart_account_for_user(user)
        fallback = trade_wallet or (accounts[0] if accounts else "")
        if sa and _position_is_agent_fill(user, position, sa):
            fallback = sa.address
        return (
            fallback,
            position.amount,
            _is_smart_account(user, fallback),
            _signer_for_holder(user, fallback),
        )
    return "", Decimal("0"), False, ""


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
        position.claim_via_agent = False
        position.claim_wallet = ""
        position.claim_payout = None
        position.closeable = False
        position.close_wallet = ""
        position.close_proceeds = None
        if position.result == "open":
            wallet = _position_wallet(user, position)
            position.close_wallet = wallet
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
                position.closeable = True
                price = position.outcome.current_price or Decimal("0")
                position.close_proceeds = (held * price).quantize(FOUR_PLACES)
            continue
        if position.result not in ("won", "void"):
            continue
        if adapter is None:
            adapter = get_adapter()
        holder, held, via_sa, signer = _resolve_claim(user, position, adapter)
        position.claim_wallet = signer or holder
        if held > 0:
            position.claimable = True
            position.claim_via_agent = bool(via_sa)
            payout = settlement_payout_price(position.event, position.outcome) or Decimal("0")
            position.claim_payout = (held * payout).quantize(FOUR_PLACES)
    return positions


def list_agent_claimable(user) -> list[Position]:
    """Won/void positions whose outcome tokens sit on the user's Smart Account."""
    rows = annotate_positions(
        user,
        list(
            Position.objects.filter(
                user=user,
                status__in=(Position.Status.OPEN, Position.Status.SETTLED),
            )
            .select_related("event", "outcome")
            .order_by("-opened_at")[:40]
        ),
    )
    return [p for p in rows if p.claimable and getattr(p, "claim_via_agent", False)]


def _to_raw_amount(human: Decimal) -> int:
    from django.conf import settings

    scale = Decimal(10) ** int(settings.DREAMDEX_COLLATERAL_DECIMALS)
    return int((human * scale).to_integral_value())


def prepare_position_redeem(user, position_id: int, wallet_address: str = ""):
    """Build the user-signed BinaryMarketsModule.redeem tx for a winning or voided position.

    Telegram / DreamAgent fills land on the Hybrid Smart Account. The owner EOA
    cannot call HybridDeleGator.execute (onlyEntryPoint), so those claims go
    through the session key's redeemDelegations — same path as /trade.
    Wallet address is required only for MetaMask-held tokens.
    """
    from django.conf import settings

    from integrations.dreamdex.adapter import get_adapter
    from services.trading_service import _quote_wallet_fees, _tx_payload

    wallet_address = (wallet_address or "").strip()
    try:
        position = Position.objects.select_related("event", "outcome").get(
            pk=position_id,
            user=user,
        )
    except Position.DoesNotExist as exc:
        raise PortfolioError("Position not found") from exc

    try:
        from services.event_service import refresh_event_from_dreamdex

        refresh_event_from_dreamdex(position.event, force=True)
        position.event.refresh_from_db()
    except Exception:
        logger.warning(
            "event refresh failed before redeem position=%s",
            position.pk,
            exc_info=True,
        )

    result = position_result(position)
    if result not in ("won", "void"):
        raise PortfolioError("This position has nothing to claim yet.")

    adapter = get_adapter()
    holder, held, via_sa, signer = _resolve_claim(user, position, adapter)
    if held <= 0 or not holder:
        raise PortfolioError("Nothing left to claim on-chain for this position.")

    if signer and signer.lower() != wallet_address.lower() and not via_sa:
        raise PortfolioError(
            "Switch MetaMask to the wallet that holds these outcome tokens."
        )

    amount_raw = _to_raw_amount(held)
    if amount_raw <= 0:
        raise PortfolioError("Claim size is below the venue lot size.")

    outcome_idx = 0 if position.outcome.outcome_type == "YES" else 1
    poke_tx = None
    finalize_tx = None
    ready = {}
    read_ready = getattr(adapter, "read_settlement_ready", None)
    if callable(read_ready):
        try:
            ready = read_ready(position.event.external_id) or {}
        except Exception:
            logger.warning(
                "on-chain settlement read failed position=%s",
                position.pk,
                exc_info=True,
            )
    sync_tx = None
    if ready:
        if not ready.get("is_resolved") and not ready.get("is_voided"):
            qid = ready.get("oracle_question_id") or position.event.oracle_question_id
            try:
                qid_int = int(str(qid or "0"))
            except (TypeError, ValueError):
                qid_int = 0
            prep_poke = getattr(adapter, "prepare_poke_oracle", None)
            if qid_int and callable(prep_poke):
                try:
                    poke_tx = prep_poke(qid_int)
                except Exception:
                    logger.warning("pokeOracle prepare failed position=%s", position.pk)
        # JS / session key run poke first, then this. No-op if already finalized.
        prep_fin = getattr(adapter, "prepare_finalize_market", None)
        if callable(prep_fin):
            try:
                finalize_tx = prep_fin(position.event.external_id)
            except Exception:
                logger.warning("finalizeMarket prepare failed position=%s", position.pk)
        if ready.get("is_voided"):
            prep_sync = getattr(adapter, "prepare_sync_settlement", None)
            if callable(prep_sync):
                try:
                    sync_tx = prep_sync(position.event.external_id)
                except Exception:
                    logger.warning("syncSettlement prepare failed position=%s", position.pk)

    approval = None
    prep_op = getattr(adapter, "prepare_outcome_operator_approval", None)
    if callable(prep_op):
        try:
            approval = prep_op(account=holder)
        except Exception as exc:
            logger.warning("outcome operator approval check failed: %s", exc)

    try:
        unsigned = adapter.prepare_redeem(
            market_id=position.event.external_id,
            account=holder,
            outcome_idx=outcome_idx,
            amount=amount_raw,
        )
    except Exception as exc:
        raise PortfolioError(str(exc)) from exc

    if via_sa:
        claimed = _claim_via_session_key(
            user,
            position,
            unsigned=unsigned,
            approval=approval,
            poke_tx=poke_tx,
            finalize_tx=finalize_tx,
            sync_tx=sync_tx,
        )
        if claimed:
            return claimed

    if via_sa:
        raise PortfolioError(
            "These winnings are on your Smart Account. Re-sign DreamAgent at "
            "/agent/activate/ so DreamLens can claim them (the owner wallet "
            "cannot call the Smart Account directly)."
        )

    if not wallet_address:
        raise PortfolioError(
            "These winnings are in MetaMask. Claim them on Portfolio with that wallet."
        )

    if signer and signer.lower() != wallet_address.lower():
        raise PortfolioError(
            "Switch MetaMask to the wallet that holds these outcome tokens."
        )

    for tx in (poke_tx, finalize_tx, sync_tx, unsigned, approval):
        if tx is None:
            continue
        try:
            _quote_wallet_fees(adapter, tx, wallet_address, estimate=False)
        except Exception:
            logger.warning("claim fee quote failed position=%s", position.pk)

    payout = settlement_payout_price(position.event, position.outcome) or Decimal("0")
    return {
        "position_id": position.pk,
        "unsigned_tx": _tx_payload(unsigned),
        "approval_tx": _tx_payload(approval) if approval else None,
        "poke_tx": _tx_payload(poke_tx) if poke_tx else None,
        "finalize_tx": _tx_payload(finalize_tx) if finalize_tx else None,
        "sync_tx": _tx_payload(sync_tx) if sync_tx else None,
        "outcome_idx": outcome_idx,
        "amount": str(held),
        "payout": str((held * payout).quantize(FOUR_PLACES)),
        "collateral_symbol": "Test USDC" if int(settings.DREAMDEX_CHAIN_ID) == 50312 else "USDso",
        "wallet_address": wallet_address,
        "holder": holder,
        "via_smart_account": False,
        "claimed": False,
    }


def _claim_via_session_key(
    user,
    position,
    *,
    unsigned,
    approval,
    poke_tx,
    finalize_tx,
    sync_tx=None,
):
    """Session-key redeemDelegations for Smart Account-held outcome tokens."""
    from integrations.metamask.execution import build_delegated_trade_execution
    from integrations.metamask.transactions import SessionKeyError, broadcast_delegated_execution
    from services.dream_agent_service import active_permission, get_session_key_agent

    agent = get_session_key_agent(user)
    if agent is None:
        return None
    permission = active_permission(agent)
    if permission is None:
        return None
    blob = permission.signed_delegation_json or {}
    if not blob:
        return None
    from integrations.metamask.delegation import (
        GRANT_MISSING_REDEEM,
        grant_allows_calldata,
        grant_allows_redeem,
    )

    if not grant_allows_redeem(blob):
        raise PortfolioError(GRANT_MISSING_REDEEM)
    approval_allowed = (
        approval
        if approval
        and approval.to
        and approval.data
        and grant_allows_calldata(blob, approval.data)
        else None
    )
    sa = agent.smart_account
    try:
        delegated = build_delegated_trade_execution(
            signed_delegation=blob,
            dreamdex_tx=unsigned,
            chain_id=sa.chain_id,
            approval_tx=approval_allowed,
        )
        extra_pre = []
        for step in (poke_tx, finalize_tx, sync_tx):
            if (
                step
                and step.to
                and step.data
                and grant_allows_calldata(blob, step.data)
            ):
                extra_pre.append((step.to, int(step.value or 0), step.data))
        if extra_pre:
            from dataclasses import replace
            from integrations.metamask.delegation import encode_redeem_delegations_calldata
            from integrations.metamask.smart_account import mock_smart_account_enabled

            pre = extra_pre + list(delegated.pre_executions or [])
            if not delegated.mock and not mock_smart_account_enabled():
                data = encode_redeem_delegations_calldata(
                    signed_delegation=blob,
                    target=delegated.inner_target,
                    call_data=delegated.inner_data,
                    value=delegated.inner_value,
                    pre_executions=pre,
                )
                delegated = replace(delegated, data=data, pre_executions=pre)
            else:
                delegated = replace(delegated, pre_executions=pre)
        tx_hash = broadcast_delegated_execution(
            delegated,
            metadata={
                "agent_id": agent.pk,
                "position_id": position.pk,
                "smart_account": sa.address,
                "source": "claim",
            },
        )
        confirm_position_redeem(user, position.pk, tx_hash)
        payout = settlement_payout_price(position.event, position.outcome) or Decimal("0")
        return {
            "position_id": position.pk,
            "claimed": True,
            "tx_hash": tx_hash,
            "via_smart_account": True,
            "holder": sa.address,
            "wallet_address": sa.owner_address,
            "payout": str((position.amount * payout).quantize(FOUR_PLACES)),
            "unsigned_tx": None,
            "approval_tx": None,
        }
    except PortfolioError:
        raise
    except SessionKeyError as exc:
        logger.warning("session-key claim failed position=%s: %s", position.pk, exc)
        text = str(exc).rstrip(".")
        if "re-sign" in text.lower() or "activate" in text.lower():
            raise PortfolioError(text + ".") from exc
        raise PortfolioError(
            text
            + ". If this grant is older, re-sign DreamAgent at /agent/activate/ "
            "so it can claim winnings."
        ) from exc
    except Exception as exc:
        logger.warning(
            "session-key claim failed position=%s: %s",
            position.pk,
            exc,
            exc_info=True,
        )
        raise PortfolioError(
            "DreamAgent could not claim this win on-chain. "
            + str(exc)[:240]
        ) from exc


def claim_agent_positions(user, *, position_id: int | None = None) -> dict:
    """Redeem Smart Account wins through DreamAgent. Skips MetaMask-held tokens."""
    from integrations.dreamdex.adapter import get_adapter
    from services.dream_agent_service import get_session_key_agent
    from services.event_copy import event_question

    agent = get_session_key_agent(user)
    if agent is None:
        raise PortfolioError(
            "Activate DreamAgent to claim Smart Account winnings. "
            "MetaMask fills still use Claim on Portfolio."
        )
    owner = (agent.smart_account.owner_address if agent.smart_account else "") or ""
    if not owner:
        raise PortfolioError("Smart Account is missing an owner address.")

    try:
        refresh_portfolio(user)
    except Exception:
        logger.warning("portfolio refresh failed before agent claim", exc_info=True)

    qs = (
        Position.objects.filter(
            user=user,
            status__in=(Position.Status.OPEN, Position.Status.SETTLED),
        )
        .select_related("event", "outcome")
        .order_by("-opened_at")
    )
    if position_id is not None:
        qs = qs.filter(pk=position_id)
        if not qs.exists():
            raise PortfolioError("Position not found.")

    adapter = get_adapter()
    claimed_rows: list[dict] = []
    skipped = 0
    for position in qs:
        if position_result(position) not in ("won", "void"):
            if position_id is not None:
                raise PortfolioError("This position has nothing to claim yet.")
            continue
        _holder, held, via_sa, _signer = _resolve_claim(user, position, adapter)
        if held <= 0 or not via_sa:
            if position_id is not None:
                raise PortfolioError(
                    "These winnings are in MetaMask. Claim them on Portfolio."
                )
            skipped += 1
            continue
        payload = prepare_position_redeem(user, position.pk, owner)
        if not payload.get("claimed"):
            skipped += 1
            if position_id is not None:
                raise PortfolioError(
                    "DreamAgent could not claim this win. Re-sign the grant at /agent/activate/."
                )
            continue
        claimed_rows.append(
            {
                "position_id": position.pk,
                "tx_hash": payload.get("tx_hash") or "",
                "payout": payload.get("payout") or "",
                "question": event_question(position.event),
                "voided": position.event.status == EventContract.Status.VOIDED,
            }
        )
    return {
        "claimed": len(claimed_rows),
        "skipped": skipped,
        "results": claimed_rows,
        "via_smart_account": True,
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
        holder, held, _via_sa, _signer = _resolve_claim(user, position, adapter)
        if not holder:
            holder = _position_wallet(user, position)
        try:
            if held <= 0:
                held = _outcome_held(
                    adapter.get_outcome_balances(holder, position.event.external_id),
                    position.outcome.outcome_type,
                )
            record(
                account=holder,
                market_id=position.event.external_id,
                outcome_idx=0 if position.outcome.outcome_type == "YES" else 1,
                amount_raw=_to_raw_amount(held if held > 0 else position.amount),
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


def prepare_position_close(user, position_id: int, wallet_address: str):
    """Build a user-signed SELL of the open position's outcome tokens."""
    from django.conf import settings

    from integrations.dreamdex.adapter import get_adapter
    from services.trading_service import TradingError, _tx_payload, prepare_sell_trade

    if not wallet_address:
        raise PortfolioError("Wallet address required")
    try:
        position = Position.objects.select_related("event", "outcome").get(
            pk=position_id,
            user=user,
        )
    except Position.DoesNotExist as exc:
        raise PortfolioError("Position not found") from exc

    if position_result(position) != "open":
        raise PortfolioError(
            "This position can only be closed while the market is still trading. "
            "After the window ends, wait for settlement and claim."
        )

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
        logger.warning("outcome balance read failed for close position=%s", position.pk)
    if held <= 0:
        raise PortfolioError("Nothing left to sell on-chain for this position.")

    try:
        trade, unsigned, approval = prepare_sell_trade(
            user=user,
            event_id=position.event_id,
            outcome=position.outcome.outcome_type,
            quantity=held,
            wallet_address=wallet_address,
        )
    except TradingError as exc:
        raise PortfolioError(str(exc)) from exc

    price = position.outcome.current_price or Decimal("0")
    qty = trade.amount
    return {
        "position_id": position.pk,
        "trade_id": trade.pk,
        "unsigned_tx": _tx_payload(unsigned),
        "approval_tx": _tx_payload(approval) if approval else None,
        "outcome": position.outcome.outcome_type,
        "amount": str(qty),
        "proceeds": str((qty * price).quantize(FOUR_PLACES)),
        "collateral_symbol": "Test USDC" if int(settings.DREAMDEX_CHAIN_ID) == 50312 else "USDso",
        "wallet_address": wallet_address,
    }


def confirm_position_close(user, position_id: int, tx_hash: str, *, trade_id: int | None = None) -> Position:
    """Record a confirmed close (sell) and refresh the position."""
    from services.trading_service import TradingError, confirm_trade

    if not tx_hash or not str(tx_hash).startswith("0x"):
        raise PortfolioError("Transaction hash required")
    try:
        position = Position.objects.select_related("event", "outcome").get(
            pk=position_id,
            user=user,
        )
    except Position.DoesNotExist as exc:
        raise PortfolioError("Position not found") from exc

    trade = None
    qs = Trade.objects.filter(
        user=user,
        event=position.event,
        outcome=position.outcome,
        side=Trade.Side.SELL,
        status__in=(
            Trade.Status.PREPARED,
            Trade.Status.AWAITING_CONFIRMATION,
            Trade.Status.SUBMITTED,
        ),
    ).order_by("-opened_at")
    if trade_id is not None:
        trade = qs.filter(pk=trade_id).first()
    if trade is None:
        trade = qs.first()
    if trade is None:
        raise PortfolioError("No pending close order for this position.")

    try:
        confirm_trade(trade.pk, tx_hash, user=user)
    except TradingError as exc:
        raise PortfolioError(str(exc)) from exc

    from integrations.dreamdex.adapter import get_adapter

    adapter = get_adapter()
    record = getattr(adapter, "record_sell", None)
    if callable(record):
        wallet = _position_wallet(user, position) or str(
            (trade.metadata_json or {}).get("wallet") or ""
        )
        try:
            record(
                account=wallet,
                market_id=position.event.external_id,
                outcome_idx=0 if position.outcome.outcome_type == "YES" else 1,
                amount=trade.amount,
            )
        except Exception:
            logger.warning("mock sell record failed position=%s", position.pk)

    position.refresh_from_db()
    logger.info(
        "position close confirmed user=%s position=%s tx=%s",
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
