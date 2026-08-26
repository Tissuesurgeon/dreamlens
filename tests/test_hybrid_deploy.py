"""Hybrid Smart Account factory deploy encoding (MetaMask ERC-1967 + initialize)."""

from __future__ import annotations

from integrations.metamask.smart_account import encode_factory_deploy_tx, owner_deploy_salt
from services.smart_account_service import grant_payload_for_ui

FACTORY = "0x476643261159d27d2ffe85cafd305c950648b317"
IMPL = "0xed13f0d784b830057d8baa808bb9989bc0e1dd92"
OWNER_A = "0x481B210d927765133d55461c3EaCC96F41FdD6C3"
OWNER_B = "0xE0588c9a06FB78f15D38785c654cDF6961697c4c"


def test_factory_deploy_tx_is_owner_specific(settings):
    settings.METAMASK_SIMPLE_FACTORY = FACTORY
    settings.METAMASK_HYBRID_IMPL = IMPL
    settings.METAMASK_DELEGATION_MANAGER = "0xf3f380e58d1742747338c46786cc7d5f9e71ef5c"
    settings.DREAMDEX_CHAIN_ID = 50312

    a = encode_factory_deploy_tx(
        implementation=IMPL,
        salt=owner_deploy_salt(OWNER_A),
        owner_address=OWNER_A,
    )
    b = encode_factory_deploy_tx(
        implementation=IMPL,
        salt=owner_deploy_salt(OWNER_B),
        owner_address=OWNER_B,
    )
    assert a["to"].lower() == FACTORY.lower()
    assert a["data"].startswith("0x")
    assert len(a["data"]) > 200
    assert a["predicted_address"].startswith("0x")
    assert a["predicted_address"].lower() != b["predicted_address"].lower()
    assert a["chain_id"] == "0xc488"
    assert a["already_deployed"] is False


def test_grant_payload_marks_already_deployed_when_code_exists(user, settings, monkeypatch):
    """CREATE2 retry must not be offered once the Hybrid proxy is on-chain."""
    settings.MOCK_SMART_ACCOUNT = False
    settings.METAMASK_SIMPLE_FACTORY = FACTORY
    settings.METAMASK_HYBRID_IMPL = IMPL
    settings.METAMASK_DELEGATION_MANAGER = "0xf3f380e58d1742747338c46786cc7d5f9e71ef5c"
    settings.DREAMDEX_CHAIN_ID = 50312
    settings.DREAM_AGENT_SESSION_KEY = "0x" + "11" * 32

    predicted = encode_factory_deploy_tx(
        implementation=IMPL,
        salt=owner_deploy_salt(OWNER_A),
        owner_address=OWNER_A,
    )["predicted_address"]

    def fake_probe(address):
        assert address.lower() == predicted.lower()
        return {
            "code_size": 163,
            "owner": OWNER_A,
            "implementation": IMPL,
        }

    monkeypatch.setattr(
        "services.smart_account_service.probe_hybrid_account",
        fake_probe,
    )
    payload = grant_payload_for_ui(user, owner_address=OWNER_A)
    assert payload["deploy_tx"]["predicted_address"].lower() == predicted.lower()
    assert payload["deploy_tx"]["already_deployed"] is True
    assert payload["smart_account"] is None


def test_grant_payload_does_not_skip_deploy_when_chain_is_empty(user, settings, monkeypatch):
    settings.MOCK_SMART_ACCOUNT = False
    settings.METAMASK_SIMPLE_FACTORY = FACTORY
    settings.METAMASK_HYBRID_IMPL = IMPL
    settings.METAMASK_DELEGATION_MANAGER = "0xf3f380e58d1742747338c46786cc7d5f9e71ef5c"
    settings.DREAMDEX_CHAIN_ID = 50312
    settings.DREAM_AGENT_SESSION_KEY = "0x" + "11" * 32

    monkeypatch.setattr(
        "services.smart_account_service.probe_hybrid_account",
        lambda address: {"code_size": 0, "owner": "", "implementation": ""},
    )
    payload = grant_payload_for_ui(user, owner_address=OWNER_A)
    assert payload["deploy_tx"]["already_deployed"] is False
