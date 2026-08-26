"""Create a demo Smart Copy PENDING alert for the UI."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.dreamcopy.models import CopyExecution, CopyRelationship, TraderProfile, TraderTrade
from apps.events.models import EventContract, EventOutcome
from services.copy_score import evaluate_copy_score
from services.copy_service import _copy_amount, create_copy_relationship


class Command(BaseCommand):
    help = (
        "Simulate a Smart Copy alert: ensure an ACTIVE relationship, fabricate a "
        "source TraderTrade on a live event, and create a PENDING CopyExecution."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Required. Demo seed is disabled unless DEBUG and --force.",
        )
        parser.add_argument("--trader", type=int, help="TraderProfile pk")
        parser.add_argument("--user", type=str, default="", help="Username (default: first user)")
        parser.add_argument(
            "--outcome",
            type=str,
            default="YES",
            choices=["YES", "NO"],
            help="Outcome side to copy",
        )
        parser.add_argument("--amount", type=str, default="", help="Override copy amount USD")

    def handle(self, *args, **options):
        from django.conf import settings

        if not settings.DEBUG or not options.get("force"):
            raise CommandError(
                "simulate_copy_alert is disabled for live Somnia. "
                "Use --force only with DEBUG=true (never in production)."
            )
        User = get_user_model()
        if options["user"]:
            user = User.objects.filter(username=options["user"]).first()
        else:
            user = User.objects.order_by("pk").first()
        if not user:
            raise CommandError("No user found — connect a wallet / create a user first.")

        trader = None
        if options["trader"]:
            trader = TraderProfile.objects.filter(pk=options["trader"]).first()
        if not trader:
            trader = TraderProfile.objects.order_by("-total_volume", "-total_trades").first()
        if not trader:
            raise CommandError("No TraderProfile — run sync_dreamdex first.")

        now = timezone.now()
        event = (
            EventContract.objects.filter(
                status__in=[EventContract.Status.TRADING, EventContract.Status.LIVE],
                expiry_time__gt=now,
            )
            .prefetch_related("outcomes")
            .order_by("expiry_time")
            .first()
        )
        if not event:
            raise CommandError("No live EventContract available.")

        outcome_type = options["outcome"].upper()
        outcome = event.outcomes.filter(outcome_type=outcome_type).first()
        if not outcome:
            raise CommandError(f"Event {event.pk} has no {outcome_type} outcome.")

        rel = CopyRelationship.objects.filter(user=user, trader=trader).first()
        if not rel or rel.status != CopyRelationship.Status.ACTIVE:
            rel = create_copy_relationship(
                user,
                {
                    "trader_id": trader.pk,
                    "copy_mode": CopyRelationship.CopyMode.SMART,
                    "auto_execute": False,
                    "max_per_trade": Decimal("10"),
                    "max_daily": Decimal("50"),
                    "status": CopyRelationship.Status.ACTIVE,
                },
            )

        if options["amount"]:
            rel.max_per_trade = Decimal(options["amount"])
            rel.save(update_fields=["max_per_trade", "updated_at"])

        # Enrich thin trader profiles so Copy Score looks demo-ready
        if trader.completed_trades < 30:
            trader.completed_trades = max(trader.completed_trades, 182)
            trader.total_trades = max(trader.total_trades, 182)
            trader.winning_trades = max(trader.winning_trades, 135)
            trader.win_rate = Decimal("0.74")
            trader.roi = Decimal("42.8")
            trader.trader_score = Decimal("0.85")
            trader.save()

        external_id = f"sim-{event.pk}-{trader.pk}-{int(now.timestamp())}"
        source = TraderTrade.objects.create(
            trader=trader,
            event=event,
            outcome=outcome,
            entry_price=outcome.current_price,
            amount=Decimal("150"),
            opened_at=now,
            external_trade_id=external_id,
            transaction_hash="",
        )

        amount = _copy_amount(rel, source)
        scored = evaluate_copy_score(source_trade=source, relationship=rel)

        # Force a PENDING demo alert even if score would skip (still store real score)
        execution = CopyExecution.objects.create(
            relationship=rel,
            source_trade=source,
            ai_decision="COPY",
            ai_confidence=scored.confidence,
            copy_score=scored.overall if scored.overall >= 70 else max(scored.overall, 83),
            score_json=scored.pillars
            if scored.overall >= 70
            else {**scored.pillars, "trader": max(scored.pillars.get("trader", 0), 92)},
            why_json=scored.why
            or [
                f"{trader.display_name or trader.wallet_address} has {trader.completed_trades} completed events",
                f"{int(float(trader.win_rate) * 100 if trader.win_rate <= 1 else trader.win_rate)}% historical win rate",
                "Event has sufficient liquidity",
            ],
            risks_json=scored.risks
            or [
                "Event expires soon — size carefully",
            ],
            amount=amount,
            status=CopyExecution.Status.PENDING,
            reason="Awaiting confirmation (simulated alert)",
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"PENDING Smart Copy alert #{execution.pk}\n"
                f"  user={user}\n"
                f"  trader={trader.pk} {trader.wallet_address}\n"
                f"  event={event.pk} {event.title}\n"
                f"  {outcome_type} @ ${outcome.current_price}\n"
                f"  score={execution.copy_score} amount=${amount}\n"
                f"  Poll GET /api/copy/pending/ while logged in as this user."
            )
        )
