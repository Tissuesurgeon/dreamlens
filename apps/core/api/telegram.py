"""Telegram link + webhook API."""

from __future__ import annotations

from django.conf import settings
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from services.telegram_bot_service import handle_update
from services.telegram_link_service import (
    TelegramLinkError,
    get_link,
    serialize_link,
    start_link,
    unlink,
)


class TelegramLinkView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(serialize_link(get_link(request.user)))

    def post(self, request):
        raw = request.data.get("chat_id") or request.data.get("telegram_chat_id") or ""
        try:
            chat_id = int(str(raw).strip())
        except (TypeError, ValueError):
            return Response(
                {"detail": "chat_id must be a number"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            link = start_link(request.user, chat_id)
        except TelegramLinkError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serialize_link(link), status=status.HTTP_201_CREATED)

    def delete(self, request):
        unlink(request.user)
        return Response(serialize_link(None), status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name="dispatch")
class TelegramWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        expected = (getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or "").strip()
        if not expected:
            return Response(
                {"detail": "Telegram webhook is not configured"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
        if incoming != expected:
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)
        handle_update(request.data if isinstance(request.data, dict) else {})
        return Response({"ok": True})
