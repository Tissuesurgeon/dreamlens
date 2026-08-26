"""Portfolio API views."""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from services.portfolio_service import (
    PortfolioError,
    annotate_positions,
    confirm_position_redeem,
    get_portfolio_summary,
    get_wallet_balances_for_user,
    list_positions,
    prepare_position_redeem,
)

from .serializers import PositionRedeemConfirmSerializer, PositionRedeemSerializer, PositionSerializer

logger = logging.getLogger("dreamlens.api.portfolio")


class PortfolioSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        summary = get_portfolio_summary(request.user)
        return Response(summary)


class PortfolioPositionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        status_filter = request.query_params.get("status")
        positions = annotate_positions(
            request.user,
            list_positions(request.user, status=status_filter),
        )
        return Response({"results": PositionSerializer(positions, many=True).data})


class PositionRedeemView(APIView):
    permission_classes = [IsAuthenticated]
    renderer_classes = [JSONRenderer]

    def post(self, request, pk: int):
        serializer = PositionRedeemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payload = prepare_position_redeem(
                request.user,
                pk,
                serializer.validated_data["wallet_address"],
            )
        except PortfolioError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("prepare redeem failed position=%s", pk)
            return Response(
                {"detail": str(exc) or "Could not build the claim transaction."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(payload)


class PositionRedeemConfirmView(APIView):
    permission_classes = [IsAuthenticated]
    renderer_classes = [JSONRenderer]

    def post(self, request, pk: int):
        serializer = PositionRedeemConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            position = confirm_position_redeem(
                request.user,
                pk,
                serializer.validated_data["tx_hash"],
            )
        except PortfolioError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("confirm redeem failed position=%s", pk)
            return Response(
                {"detail": str(exc) or "Could not record the claim."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"position": PositionSerializer(position).data})


class PortfolioBalancesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        balances = get_wallet_balances_for_user(request.user)
        if balances is None:
            return Response(
                {"detail": "No wallet linked to this session."},
                status=400,
            )
        return Response(balances)