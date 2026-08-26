"""Event contract API views."""

from __future__ import annotations

from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from django.utils import timezone

from apps.events.models import EventContract, EventRadarSignal, EventSnapshot
from services.consensus_service import compute_consensus, save_consensus_snapshot
from services.event_service import refresh_event_from_dreamdex, refresh_events_from_dreamdex

from .serializers import (
    ConsensusSnapshotSerializer,
    EventContractSerializer,
    EventRadarSignalSerializer,
    EventSnapshotSerializer,
)


class EventListView(generics.ListAPIView):
    serializer_class = EventContractSerializer

    def get_queryset(self):
        refresh_events_from_dreamdex()
        qs = EventContract.objects.prefetch_related("outcomes").order_by("-updated_at")
        asset = self.request.query_params.get("asset")
        status_filter = self.request.query_params.get("status")
        if asset:
            qs = qs.filter(underlying_asset__iexact=asset)
        if status_filter:
            qs = qs.filter(status__iexact=status_filter)
        else:
            qs = qs.filter(
                status__in=[EventContract.Status.TRADING, EventContract.Status.LIVE],
                expiry_time__gt=timezone.now(),
            )
        return qs


class EventDetailView(generics.RetrieveAPIView):
    queryset = EventContract.objects.prefetch_related("outcomes")
    serializer_class = EventContractSerializer

    def get_object(self):
        event = super().get_object()
        event = refresh_event_from_dreamdex(event)
        return (
            EventContract.objects.prefetch_related("outcomes")
            .filter(pk=event.pk)
            .first()
            or event
        )


class EventHistoryView(generics.ListAPIView):
    serializer_class = EventSnapshotSerializer

    def get_queryset(self):
        return EventSnapshot.objects.filter(event_id=self.kwargs["pk"]).order_by("-timestamp")


class EventRadarView(generics.ListAPIView):
    serializer_class = EventRadarSignalSerializer

    def get_queryset(self):
        return (
            EventRadarSignal.objects.filter(is_active=True)
            .select_related("event")
            .prefetch_related("event__outcomes")
            .order_by("-score", "-created_at")
        )


class EventConsensusView(APIView):
    def get(self, request, pk: int):
        try:
            event = EventContract.objects.prefetch_related("outcomes").get(pk=pk)
        except EventContract.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        data = compute_consensus(event)
        snapshot = save_consensus_snapshot(event)
        payload = ConsensusSnapshotSerializer(snapshot).data
        payload["agreement_level_label"] = data["agreement_level"]
        payload["disclaimer"] = data["disclaimer"]
        payload["live"] = {
            "yes_consensus": str(data["yes_consensus"]),
            "no_consensus": str(data["no_consensus"]),
            "trader_count": data["trader_count"],
            "agreement_level": data["agreement_level"],
        }
        return Response(payload)
