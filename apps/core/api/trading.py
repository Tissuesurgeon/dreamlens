"""Trading API views."""

from __future__ import annotations

from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.trading.models import Trade
from services.trading_service import TradingError, _tx_payload, confirm_trade, prepare_trade

from .serializers import ConfirmTradeSerializer, PrepareTradeSerializer, TradeSerializer


class TradeListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TradeSerializer

    def get_queryset(self):
        return (
            Trade.objects.filter(user=self.request.user)
            .select_related("event", "outcome")
            .order_by("-opened_at")
        )


class TradeDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TradeSerializer

    def get_queryset(self):
        return Trade.objects.filter(user=self.request.user).select_related("event", "outcome")


class PrepareTradeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PrepareTradeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            trade, unsigned, approval = prepare_trade(
                user=request.user,
                event_id=data["event_id"],
                outcome=data["outcome"],
                amount=data["amount"],
                wallet_address=data["wallet_address"],
                amount_is_notional=True,
            )
        except TradingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "trade": TradeSerializer(trade).data,
                "trade_id": trade.pk,
                "unsigned_tx": _tx_payload(unsigned) if unsigned else None,
                "approval_tx": _tx_payload(approval) if approval else None,
            },
            status=status.HTTP_201_CREATED,
        )


class ConfirmTradeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ConfirmTradeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            trade = confirm_trade(
                trade_id=data["trade_id"],
                tx_hash=data["tx_hash"],
                user=request.user,
            )
        except TradingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"trade": TradeSerializer(trade).data})
