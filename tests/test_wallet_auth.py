"""Wallet session login for Following / copy pages."""

from __future__ import annotations

import pytest

from apps.accounts.models import User, Wallet


@pytest.mark.django_db
def test_wallet_login_creates_user_and_unlocks_following(client, settings):
    address = "0xAbCdEf0000000000000000000000000000000001"
    before = client.get("/following/")
    assert b"Connect your wallet to follow traders" in before.content

    res = client.post(
        "/api/auth/wallet/",
        data={"address": address, "chain_id": settings.DREAMDEX_CHAIN_ID},
        content_type="application/json",
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is True
    assert payload["address"] == address.lower()

    user = User.objects.get(username=address.lower())
    assert Wallet.objects.filter(user=user, address=address.lower()).exists()

    after = client.get("/following/")
    assert b"Connect your wallet to follow traders" not in after.content
    assert b"Smart Copy" in after.content


@pytest.mark.django_db
def test_wallet_login_rejects_invalid_address(client):
    res = client.post(
        "/api/auth/wallet/",
        data={"address": "not-a-wallet"},
        content_type="application/json",
    )
    assert res.status_code == 400


@pytest.mark.django_db
def test_wallet_relogin_while_authenticated_is_not_csrf_blocked(user, settings):
    """Browser already has a session cookie; MetaMask restore POSTs /api/auth/wallet/ again."""
    from rest_framework.test import APIClient

    address = "0xabcdef0000000000000000000000000000000001"
    Wallet.objects.create(
        user=user,
        address=address,
        chain_id=settings.DREAMDEX_CHAIN_ID,
        is_primary=True,
    )
    api = APIClient(enforce_csrf_checks=True)
    api.force_login(user)
    res = api.post(
        "/api/auth/wallet/",
        {"address": address, "chain_id": settings.DREAMDEX_CHAIN_ID},
        format="json",
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True


@pytest.mark.django_db
def test_wallet_session_can_register_smart_account(client, settings):
    address = "0xAbCdEf0000000000000000000000000000000001"
    login = client.post(
        "/api/auth/wallet/",
        data={"address": address, "chain_id": settings.DREAMDEX_CHAIN_ID},
        content_type="application/json",
    )
    assert login.status_code == 200

    res = client.post(
        "/api/smart-account/",
        data={
            "owner_address": address,
            "address": "0x476643261159d27d2ffe85cafd305c950648b317",
            "factory_address": "0x476643261159d27d2ffe85cafd305c950648b317",
        },
        content_type="application/json",
    )
    assert res.status_code == 201
    assert res.json()["smart_account"]["owner_address"].lower() == address.lower()
