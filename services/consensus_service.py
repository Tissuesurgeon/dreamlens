"""Trader consensus computation — informational only, not a guarantee."""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from apps.analytics.models import ConsensusSnapshot
from apps.dreamcopy.models import TraderTrade
from apps.events.models import EventContract, EventOutcome

logger = logging.getLogger("dreamlens.services.consensus")

FOUR_PLACES = Decimal("0.0001")
DISCLAIMER = (
    "Trader consensus is a snapshot of indexed activity — not a guarantee of outcome."
)


def agreement_label(score: Decimal) -> str:
    if score >= Decimal("0.70"):
        return "HIGH"
    if score >= Decimal("0.40"):
        return "MEDIUM"
    return "LOW"


def compute_consensus(event: EventContract) -> dict:
    """
    Compute yes/no consensus from recent trader trades on this event.
    Returns dict with consensus values and agreement label.
    """
    trades = (
        TraderTrade.objects.filter(event=event)
        .select_related("outcome")
        .order_by("-opened_at")[:100]
    )

    yes_weight = Decimal("0")
    no_weight = Decimal("0")
    wallets: set[str] = set()

    for trade in trades:
        wallets.add(trade.trader_id)
        weight = trade.amount * trade.entry_price
        if trade.outcome.outcome_type == EventOutcome.OutcomeType.YES:
            yes_weight += weight
        else:
            no_weight += weight

    total = yes_weight + no_weight
    if total > 0:
        yes_consensus = (yes_weight / total).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
        no_consensus = (Decimal("1") - yes_consensus).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
    else:
        yes_outcome = event.outcomes.filter(outcome_type=EventOutcome.OutcomeType.YES).first()
        yes_price = yes_outcome.current_price if yes_outcome else Decimal("0.5")
        yes_consensus = yes_price.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
        no_consensus = (Decimal("1") - yes_consensus).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)

    trader_count = len(wallets)
    if trader_count == 0:
        agreement_score = Decimal("0")
    else:
        dominant = max(yes_consensus, no_consensus)
        sample_factor = min(Decimal(trader_count) / Decimal("10"), Decimal("1"))
        agreement_score = (dominant * sample_factor).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)

    level = agreement_label(agreement_score)

    return {
        "yes_consensus": yes_consensus,
        "no_consensus": no_consensus,
        "trader_count": trader_count,
        "agreement_level": level,
        "agreement_score": agreement_score,
        "disclaimer": DISCLAIMER,
    }


@transaction.atomic
def save_consensus_snapshot(event: EventContract) -> ConsensusSnapshot:
    data = compute_consensus(event)
    snapshot = ConsensusSnapshot.objects.create(
        event=event,
        yes_consensus=data["yes_consensus"],
        no_consensus=data["no_consensus"],
        trader_count=data["trader_count"],
        agreement_level=data["agreement_score"],
    )
    logger.info("save_consensus_snapshot event=%s traders=%s", event.pk, data["trader_count"])
    return snapshot


@transaction.atomic
def compute_consensus_for_all_active() -> dict[str, int]:
    """Compute and persist consensus for all tradable events."""
    events = EventContract.objects.filter(
        status__in=[
            EventContract.Status.TRADING,
            EventContract.Status.LIVE,
        ]
    )
    count = 0
    for event in events:
        save_consensus_snapshot(event)
        count += 1
    return {"snapshots_created": count}
