"""AI lens API views."""

from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.events.models import EventContract
from services import ai_service

from .serializers import AnalyzeEventSerializer, ChatSerializer, SearchSerializer


class ChatView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ChatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = ai_service.chat(
            message=serializer.validated_data["message"],
            user=request.user if request.user.is_authenticated else None,
            event_id=serializer.validated_data.get("event_id"),
        )
        return Response(result)


class AnalyzeEventView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = AnalyzeEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            event = EventContract.objects.prefetch_related("outcomes").get(
                pk=serializer.validated_data["event_id"]
            )
        except EventContract.DoesNotExist:
            return Response({"detail": "Event not found."}, status=status.HTTP_404_NOT_FOUND)

        result = ai_service.analyze_event(
            event,
            user=request.user if request.user.is_authenticated else None,
        )
        return Response(result)


class SearchView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SearchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        events = ai_service.search_events(
            serializer.validated_data.get("query", ""),
            limit=serializer.validated_data.get("limit", 20),
        )
        return Response({"results": events, "count": len(events)})
