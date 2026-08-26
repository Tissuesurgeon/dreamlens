"""Trader profile API views."""

from __future__ import annotations

from rest_framework import generics

from apps.dreamcopy.models import TraderProfile, TraderTrade
from services.trader_service import is_onchain_trader_wallet, list_active_traders

from .serializers import TraderProfileSerializer, TraderTradeSerializer


class TraderListView(generics.ListAPIView):
    serializer_class = TraderProfileSerializer

    def get_queryset(self):
        suggested = list_active_traders()
        ids = [t.pk for t in suggested if is_onchain_trader_wallet(t.wallet_address)]
        qs = TraderProfile.objects.filter(pk__in=ids).order_by(
            "-total_volume", "-total_trades", "-trader_score"
        )
        min_score = self.request.query_params.get("min_score")
        if min_score:
            qs = qs.filter(trader_score__gte=min_score)
        return qs


class TraderDetailView(generics.RetrieveAPIView):
    queryset = TraderProfile.objects.all()
    serializer_class = TraderProfileSerializer


class TraderHistoryView(generics.ListAPIView):
    serializer_class = TraderTradeSerializer

    def get_queryset(self):
        return (
            TraderTrade.objects.filter(trader_id=self.kwargs["pk"])
            .select_related("event", "outcome")
            .order_by("-opened_at")
        )
