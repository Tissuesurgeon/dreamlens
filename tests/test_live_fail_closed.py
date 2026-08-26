"""Fail-closed live Smart Account / AI paths (pytest still forces mocks by default)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command

from services import smart_account_service
from services.ai_service import UnavailableLLMClient, get_llm_client
from services.smart_account_service import SmartAccountError
from integrations.metamask.smart_account import SmartAccountConfigError

OWNER = "0x481B210d927765133d55461c3EaCC96F41FdD6C3"
SA = "0x476643261159d27d2ffe85cafd305c950648b317"


@pytest.mark.django_db
def test_live_create_requires_factory_address(user, settings):
    settings.MOCK_SMART_ACCOUNT = False
    settings.METAMASK_DELEGATION_MANAGER = ""
    settings.METAMASK_SIMPLE_FACTORY = ""
    with pytest.raises(SmartAccountConfigError):
        smart_account_service.create_account(
            user,
            owner_address=OWNER,
            address=SA,
        )


@pytest.mark.django_db
def test_live_create_requires_client_address(user, settings):
    settings.MOCK_SMART_ACCOUNT = False
    settings.METAMASK_DELEGATION_MANAGER = "0x" + "aa" * 20
    settings.METAMASK_SIMPLE_FACTORY = "0x" + "bb" * 20
    with pytest.raises(SmartAccountError, match="Hybrid account address"):
        smart_account_service.create_account(
            user,
            owner_address=OWNER,
        )


@pytest.mark.django_db
def test_live_grant_rejects_mock_signature(user, settings):
    settings.MOCK_SMART_ACCOUNT = False
    settings.METAMASK_DELEGATION_MANAGER = "0x" + "aa" * 20
    settings.METAMASK_SIMPLE_FACTORY = "0x" + "bb" * 20
    settings.DREAM_AGENT_SESSION_KEY = "0x" + "11" * 32
    sa = smart_account_service.create_account(
        user,
        owner_address=OWNER,
        address=SA,
    )
    smart_account_service.mark_funded(sa, amount=Decimal("50"))
    with pytest.raises(SmartAccountError, match="Mock"):
        smart_account_service.grant_agent(
            user,
            signed_delegation={
                "delegate": "0x" + "11" * 20,
                "delegator": sa.address,
                "authority": "0x" + "ff" * 32,
                "signature": "0xmockdeadbeef",
            },
        )


@pytest.mark.django_db
def test_live_deposit_rejects_mock_hash(user, settings):
    settings.MOCK_SMART_ACCOUNT = False
    settings.METAMASK_DELEGATION_MANAGER = "0x" + "aa" * 20
    settings.METAMASK_SIMPLE_FACTORY = "0x" + "bb" * 20
    sa = smart_account_service.create_account(
        user,
        owner_address=OWNER,
        address=SA,
    )
    with pytest.raises(SmartAccountError, match="real Somnia"):
        smart_account_service.verify_deposit_tx(tx_hash="0xmockdeposit", smart_account=sa)


def test_live_llm_unavailable_without_provider(settings):
    settings.MOCK_DREAMDEX = False
    settings.LLM_API_KEY = ""
    settings.LOCAL_LLM_ENABLED = False
    client = get_llm_client()
    assert isinstance(client, UnavailableLLMClient)


@pytest.mark.django_db
def test_simulate_copy_alert_requires_force(settings):
    settings.DEBUG = True
    with pytest.raises(CommandError, match="disabled"):
        call_command("simulate_copy_alert")
