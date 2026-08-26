"""DreamCopy / Smart Copy API views."""

from __future__ import annotations

from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.dreamcopy.models import CopyExecution, CopyRelationship
from services.copy_service import (
    CopyError,
    create_copy_relationship,
    delete_copy_relationship,
    prepare_copy_trade,
    serialize_execution,
    skip_copy_execution,
    update_copy_relationship,
)
from services.trading_service import _tx_payload

from .serializers import CopyRelationshipSerializer


class CopyRelationshipListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = CopyRelationshipSerializer

    def get_queryset(self):
        return CopyRelationship.objects.filter(user=self.request.user).select_related(
            "trader"
        )

    def perform_create(self, serializer):
        rel = create_copy_relationship(
            self.request.user,
            serializer.validated_data,
        )
        serializer.instance = rel


class CopyRelationshipDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = CopyRelationshipSerializer
    http_method_names = ["get", "patch", "delete"]

    def get_queryset(self):
        return CopyRelationship.objects.filter(user=self.request.user).select_related(
            "trader"
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        rel = update_copy_relationship(instance, serializer.validated_data)
        return Response(self.get_serializer(rel).data)

    def destroy(self, request, *args, **kwargs):
        rel = self.get_object()
        delete_copy_relationship(rel)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CopyPendingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            CopyExecution.objects.filter(
                relationship__user=request.user,
                status=CopyExecution.Status.PENDING,
            )
            .select_related(
                "relationship",
                "relationship__trader",
                "source_trade",
                "source_trade__event",
                "source_trade__outcome",
            )
            .order_by("-created_at")[:20]
        )
        return Response({"results": [serialize_execution(e) for e in qs]})


class CopyActivityView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            CopyExecution.objects.filter(relationship__user=request.user)
            .exclude(status=CopyExecution.Status.PENDING)
            .select_related(
                "relationship",
                "relationship__trader",
                "source_trade",
                "source_trade__event",
                "source_trade__outcome",
            )
            .order_by("-created_at")[:50]
        )
        return Response({"results": [serialize_execution(e) for e in qs]})


class CopyExecutionSkipView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        try:
            execution = skip_copy_execution(pk, user=request.user)
        except CopyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serialize_execution(execution))


class CopyExecutionPrepareView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        wallet = request.data.get("wallet_address") or ""
        if not wallet:
            return Response(
                {"detail": "wallet_address is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            execution, trade, unsigned, approval = prepare_copy_trade(
                pk, user=request.user, wallet_address=wallet
            )
        except CopyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        payload = {
            "execution": serialize_execution(execution),
            "trade_id": trade.pk,
            "unsigned_tx": _tx_payload(unsigned) if unsigned else None,
            "approval_tx": _tx_payload(approval) if approval else None,
        }
        return Response(payload)
