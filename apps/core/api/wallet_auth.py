"""Bind a connected browser wallet to a Django session."""

from __future__ import annotations

import re

from django.conf import settings
from django.contrib.auth import login, logout
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User, Wallet
from apps.core.authentication import CsrfExemptSessionAuthentication

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def normalize_address(raw: str) -> str:
    address = (raw or "").strip()
    if not ADDRESS_RE.match(address):
        raise ValueError("Invalid wallet address")
    return address.lower()


@transaction.atomic
def login_wallet(django_request, *, address: str, chain_id: int) -> User:
    """Create or reuse a user for this wallet and start a Django session."""
    address = normalize_address(address)
    wallet = (
        Wallet.objects.select_related("user")
        .filter(address__iexact=address)
        .order_by("-is_primary")
        .first()
    )
    if wallet:
        user = wallet.user
        Wallet.objects.get_or_create(
            user=user,
            address=address,
            chain_id=chain_id,
            defaults={"is_primary": False},
        )
    else:
        user, created = User.objects.get_or_create(username=address)
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        Wallet.objects.get_or_create(
            user=user,
            address=address,
            chain_id=chain_id,
            defaults={"is_primary": True},
        )
    login(django_request, user, backend="django.contrib.auth.backends.ModelBackend")
    return user


class WalletLoginView(APIView):
    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [AllowAny]

    def post(self, request):
        raw = request.data.get("address") or request.data.get("wallet_address") or ""
        try:
            address = normalize_address(str(raw))
        except ValueError:
            return Response({"detail": "Invalid wallet address"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            chain_id = int(request.data.get("chain_id") or 0)
        except (TypeError, ValueError):
            chain_id = 0
        if chain_id <= 0:
            chain_id = int(settings.DREAMDEX_CHAIN_ID)

        user = login_wallet(request._request, address=address, chain_id=chain_id)
        return Response(
            {
                "ok": True,
                "address": address,
                "username": user.username,
            }
        )


class WalletLogoutView(APIView):
    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [AllowAny]

    def post(self, request):
        logout(request._request)
        return Response({"ok": True})
