"""DRF serializers for DreamLens API."""

from __future__ import annotations

from rest_framework import serializers

from apps.analytics.models import ConsensusSnapshot
from apps.dreamcopy.models import CopyExecution, CopyRelationship, TraderProfile, TraderTrade
from apps.events.models import EventContract, EventOutcome, EventRadarSignal, EventSnapshot
from apps.portfolio.models import Position
from apps.trading.models import Trade
from services.consensus_service import agreement_label


class EventOutcomeSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventOutcome
        fields = (
            "id",
            "outcome_type",
            "external_identifier",
            "symbol",
            "current_price",
        )


class EventContractSerializer(serializers.ModelSerializer):
    outcomes = EventOutcomeSerializer(many=True, read_only=True)
    minutes_to_expiry = serializers.FloatField(read_only=True)

    class Meta:
        model = EventContract
        fields = (
            "id",
            "external_id",
            "title",
            "description",
            "underlying_asset",
            "event_type",
            "status",
            "expiry_time",
            "trading_start",
            "cumulative_quote_volume",
            "last_price",
            "trade_count",
            "minutes_to_expiry",
            "outcomes",
            "created_at",
            "updated_at",
        )


class EventSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventSnapshot
        fields = (
            "id",
            "yes_price",
            "no_price",
            "volume",
            "liquidity",
            "timestamp",
        )


class EventRadarSignalSerializer(serializers.ModelSerializer):
    event = EventContractSerializer(read_only=True)

    class Meta:
        model = EventRadarSignal
        fields = (
            "id",
            "event",
            "signal_type",
            "score",
            "explanation",
            "details",
            "is_active",
            "created_at",
        )


class ConsensusSnapshotSerializer(serializers.ModelSerializer):
    agreement_level_label = serializers.SerializerMethodField()
    disclaimer = serializers.SerializerMethodField()

    class Meta:
        model = ConsensusSnapshot
        fields = (
            "id",
            "event_id",
            "yes_consensus",
            "no_consensus",
            "trader_count",
            "agreement_level",
            "agreement_level_label",
            "disclaimer",
            "created_at",
        )

    def get_agreement_level_label(self, obj: ConsensusSnapshot) -> str:
        return agreement_label(obj.agreement_level)

    def get_disclaimer(self, _obj: ConsensusSnapshot) -> str:
        return "Trader consensus is informational — not a guarantee of outcome."


class TraderProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = TraderProfile
        fields = (
            "id",
            "wallet_address",
            "display_name",
            "avatar",
            "total_trades",
            "completed_trades",
            "winning_trades",
            "losing_trades",
            "win_rate",
            "total_volume",
            "realized_pnl",
            "roi",
            "trader_score",
            "last_active_at",
        )


class TraderTradeSerializer(serializers.ModelSerializer):
    event_title = serializers.CharField(source="event.title", read_only=True)
    outcome_type = serializers.CharField(source="outcome.outcome_type", read_only=True)

    class Meta:
        model = TraderTrade
        fields = (
            "id",
            "event_id",
            "event_title",
            "outcome_type",
            "entry_price",
            "amount",
            "opened_at",
            "closed_at",
            "result",
            "pnl",
            "transaction_hash",
        )


class TradeSerializer(serializers.ModelSerializer):
    event_title = serializers.CharField(source="event.title", read_only=True)
    outcome_type = serializers.CharField(source="outcome.outcome_type", read_only=True)

    class Meta:
        model = Trade
        fields = (
            "id",
            "event_id",
            "event_title",
            "outcome_type",
            "side",
            "amount",
            "entry_price",
            "status",
            "transaction_hash",
            "opened_at",
            "settled_at",
            "result",
            "pnl",
            "metadata_json",
        )
        read_only_fields = fields


class PrepareTradeSerializer(serializers.Serializer):
    event_id = serializers.IntegerField()
    outcome = serializers.ChoiceField(choices=["YES", "NO"])
    amount = serializers.DecimalField(max_digits=24, decimal_places=8)
    wallet_address = serializers.CharField(max_length=42)


class ConfirmTradeSerializer(serializers.Serializer):
    trade_id = serializers.IntegerField()
    tx_hash = serializers.CharField(max_length=66)


class CopyRelationshipSerializer(serializers.ModelSerializer):
    trader = TraderProfileSerializer(read_only=True)
    trader_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = CopyRelationship
        fields = (
            "id",
            "trader",
            "trader_id",
            "status",
            "copy_mode",
            "max_per_trade",
            "max_daily",
            "minimum_confidence",
            "min_copy_score",
            "min_win_rate",
            "min_completed_events",
            "min_liquidity",
            "min_consensus",
            "consider_json",
            "allowed_assets_json",
            "auto_execute",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "trader", "created_at", "updated_at")

    def validate(self, attrs):
        if self.instance is None and not attrs.get("trader_id"):
            raise serializers.ValidationError({"trader_id": "This field is required."})
        return attrs


class CopyExecutionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CopyExecution
        fields = (
            "id",
            "relationship_id",
            "source_trade_id",
            "copied_trade_id",
            "ai_decision",
            "ai_confidence",
            "copy_score",
            "score_json",
            "why_json",
            "risks_json",
            "amount",
            "status",
            "reason",
            "created_at",
        )


class PositionSerializer(serializers.ModelSerializer):
    event_title = serializers.CharField(source="event.title", read_only=True)
    outcome_type = serializers.CharField(source="outcome.outcome_type", read_only=True)
    result = serializers.SerializerMethodField()
    claimable = serializers.SerializerMethodField()

    class Meta:
        model = Position
        fields = (
            "id",
            "event_id",
            "event_title",
            "outcome_type",
            "amount",
            "entry_price",
            "current_value",
            "status",
            "pnl",
            "opened_at",
            "settled_at",
            "result",
            "claimable",
        )

    def get_result(self, obj):
        return getattr(obj, "result", None)

    def get_claimable(self, obj):
        return bool(getattr(obj, "claimable", False))


class PositionRedeemSerializer(serializers.Serializer):
    wallet_address = serializers.CharField(max_length=42)


class PositionRedeemConfirmSerializer(serializers.Serializer):
    tx_hash = serializers.CharField(max_length=66)


class ChatSerializer(serializers.Serializer):
    message = serializers.CharField(required=False, allow_blank=True, default="")
    query = serializers.CharField(required=False, allow_blank=True, default="")
    event_id = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        text = (attrs.get("message") or attrs.get("query") or "").strip()
        if not text:
            raise serializers.ValidationError({"message": "Ask a question about a market."})
        attrs["message"] = text
        return attrs


class LensChatSerializer(serializers.Serializer):
    message = serializers.CharField()
    event_id = serializers.IntegerField(required=False, allow_null=True)
    structured = serializers.BooleanField(required=False, default=False)
    history = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        default=list,
    )

    def validate_message(self, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise serializers.ValidationError("Ask a question about the market.")
        return text

    def validate_history(self, value):
        cleaned = []
        for item in (value or [])[-10:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").lower()
            content = str(item.get("content") or item.get("text") or "").strip()
            if role in {"user", "assistant"} and content:
                cleaned.append({"role": role, "content": content[:4000]})
        return cleaned


class AnalyzeEventSerializer(serializers.Serializer):
    event_id = serializers.IntegerField()


class SearchSerializer(serializers.Serializer):
    query = serializers.CharField(required=False, allow_blank=True, default="")
    limit = serializers.IntegerField(required=False, default=20, min_value=1, max_value=100)
