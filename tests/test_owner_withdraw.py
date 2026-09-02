"""Owner Smart Account withdraw — destination, cap, live fail-closed."""

from __future__ import annotations

from decimal import Decimal

import pytest
from eth_utils import function_signature_to_4byte_selector

from integrations.metamask.delegation import (
    ALLOWED_CALL_SELECTORS,
    TRANSFER_SELECTOR,
    build_grant_typed_data,
)
from services import smart_account_service
from services.smart_account_service import SmartAccountError

OWNER = "0x481B210d927765133d55461c3EaCC96F41FdD6C3"
SA = "0x476643261159d27d2ffe85cafd305c950648b317"
OTHER = "0x1111111111111111111111111111111111111111"


@pytest.fixture
def funded_sa(user, settings):
    settings.MOCK_SMART_ACCOUNT = True
    settings.METAMASK_DELEGATION_MANAGER = "0xf3f380e58d1742747338c46786cc7d5f9e71ef5c"
    sa = smart_account_service.create_account(
        user,
        owner_address=OWNER,
        address=SA,
    )
    return smart_account_service.mark_funded(sa, amount=Decimal("50"))


@pytest.mark.django_db
def test_prepare_withdraw_destination_is_always_owner(funded_sa, user):
    payload = smart_account_service.prepare_owner_withdraw(user, Decimal("10"))
    assert payload["destination"].lower() == OWNER.lower()
    assert payload["typed_data"]["message"]["delegate"].lower() == OWNER.lower()
    assert payload["typed_data"]["message"]["delegator"].lower() == SA.lower()
    assert payload["typed_data"]["dreamlens"]["permission"] == "OWNER_WITHDRAW"


@pytest.mark.django_db
def test_prepare_withdraw_caps_to_available(funded_sa, user):
    with pytest.raises(SmartAccountError, match="exceeds available"):
        smart_account_service.prepare_owner_withdraw(user, Decimal("99"))


@pytest.mark.django_db
def test_prepare_withdraw_assemble_inner_transfer_goes_to_owner(funded_sa, user):
    prep = smart_account_service.prepare_owner_withdraw(user, Decimal("12"))
    assembled = smart_account_service.prepare_owner_withdraw(
        user,
        Decimal("12"),
        signature="0x" + "ab" * 65,
        salt=prep["salt"],
        expires_at=prep["expires_at"],
    )
    assert assembled["unsigned_tx"]["from"].lower() == OWNER.lower()
    data = assembled["unsigned_tx"]["data"].lower()
    assert OWNER[2:].lower() in data
    assert OTHER[2:].lower() not in data


@pytest.mark.django_db
def test_confirm_withdraw_records_metadata_in_mock(funded_sa, user):
    result = smart_account_service.confirm_owner_withdraw(
        user,
        tx_hash="0xmockwithdraw",
        amount=Decimal("8"),
    )
    funded_sa.refresh_from_db()
    meta = funded_sa.metadata_json or {}
    assert meta["last_withdraw"] == "8"
    assert meta["last_withdraw_tx"] == "0xmockwithdraw"
    assert result["destination"].lower() == OWNER.lower()
    assert Decimal(str(result["balance"]["collateral"])) == Decimal("42")


@pytest.mark.django_db
def test_live_withdraw_rejects_mock_hash(user, settings):
    settings.MOCK_SMART_ACCOUNT = False
    settings.METAMASK_DELEGATION_MANAGER = "0x" + "aa" * 20
    settings.METAMASK_SIMPLE_FACTORY = "0x" + "bb" * 20
    sa = smart_account_service.create_account(
        user,
        owner_address=OWNER,
        address=SA,
    )
    with pytest.raises(SmartAccountError, match="real Somnia"):
        smart_account_service.verify_withdraw_tx(
            tx_hash="0xmockwithdraw",
            smart_account=sa,
        )


@pytest.mark.django_db
def test_live_confirm_withdraw_rejects_mock_hash(user, settings):
    settings.MOCK_SMART_ACCOUNT = False
    settings.METAMASK_DELEGATION_MANAGER = "0x" + "aa" * 20
    settings.METAMASK_SIMPLE_FACTORY = "0x" + "bb" * 20
    smart_account_service.create_account(
        user,
        owner_address=OWNER,
        address=SA,
    )
    with pytest.raises(SmartAccountError, match="real Somnia"):
        smart_account_service.confirm_owner_withdraw(
            user,
            tx_hash="0xmockdeadbeef",
            amount=Decimal("1"),
        )


def test_agent_grant_typed_data_has_no_transfer_selector():
    typed = build_grant_typed_data(
        chain_id=50312,
        delegator=SA,
        delegate=OWNER,
        verifying_contract="0xf3f380e58d1742747338c46786cc7d5f9e71ef5c",
        max_trade_amount="10",
        max_daily_volume="50",
        expires_at=2_000_000_000,
        salt=1,
    )
    transfer = function_signature_to_4byte_selector("transfer(address,uint256)")
    assert transfer == TRANSFER_SELECTOR
    assert transfer not in ALLOWED_CALL_SELECTORS
    methods_terms = typed["message"]["caveats"][0]["terms"]
    raw = bytes.fromhex(methods_terms[2:])
    selectors = {raw[i : i + 4] for i in range(0, len(raw), 4)}
    assert transfer not in selectors


@pytest.mark.django_db
def test_withdraw_api_prepare_and_confirm(client, user, funded_sa):
    client.force_login(user)
    res = client.get("/api/smart-account/withdraw/", {"amount": "5"})
    assert res.status_code == 200
    body = res.json()
    assert body["destination"].lower() == OWNER.lower()
    assert "typed_data" in body
    assert "unsigned_tx" not in body

    assembled = client.post(
        "/api/smart-account/withdraw/",
        {
            "amount": "5",
            "signature": "0x" + "ab" * 65,
            "salt": body["salt"],
            "expires_at": body["expires_at"],
        },
        content_type="application/json",
    )
    assert assembled.status_code == 200
    assert assembled.json()["unsigned_tx"]["from"].lower() == OWNER.lower()

    confirmed = client.post(
        "/api/smart-account/withdraw/",
        {"amount": "5", "tx_hash": "0x" + "cd" * 32},
        content_type="application/json",
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["destination"].lower() == OWNER.lower()
