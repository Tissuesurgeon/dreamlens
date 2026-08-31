"""DreamAgent Policy Engine, lifecycle, and delegated mock execution tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.agents.models import AgentEvaluation, DreamAgent, DreamAgentPermission
from apps.dreamcopy.models import CopyExecution
from services import dream_agent_service, smart_account_service
from services.copy_service import detect_and_process_copy
from services.policy_service import PolicyContext, PolicyEngine
from services.smart_account_service import SmartAccountError


@pytest.fixture
def smart_account(user, wallet, settings):
    settings.MOCK_SMART_ACCOUNT = True
    return smart_account_service.create_account(
        user,
        owner_address=wallet.address,
    )


@pytest.fixture
def running_agent(user, smart_account, trader, settings):
    settings.MOCK_SMART_ACCOUNT = True
    smart_account_service.mark_funded(smart_account, amount=Decimal("50"))
    agent, perm = smart_account_service.grant_agent(
        user,
        max_trade_amount=Decimal("10"),
        max_daily_volume=Decimal("50"),
        expires_in_days=30,
        min_copy_score=50,
        allowed_traders=[str(trader.pk)],
        signed_delegation={
            "delegate": "0xSession00000000000000000000000000000001",
            "delegator": smart_account.address,
            "authority": "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            "caveats": [],
            "salt": "0x1",
            "signature": "0xmockdeadbeef",
            "mock": True,
        },
        activate=True,
    )
    assert agent.status == DreamAgent.Status.RUNNING
    assert perm.status == DreamAgentPermission.Status.ACTIVE
    return agent


@pytest.mark.django_db
def test_policy_rejects_low_copy_score(running_agent, source_trade, copy_relationship):
    perm = DreamAgentPermission.objects.get(agent=running_agent)
    perm.min_copy_score = 90
    perm.save(update_fields=["min_copy_score"])
    result = PolicyEngine().evaluate(
        PolicyContext(
            agent=running_agent,
            permission=perm,
            source_trade=source_trade,
            relationship=copy_relationship,
            copy_score=60,
            amount=Decimal("8"),
            daily_volume=Decimal("0"),
        )
    )
    assert not result.ok
    assert any("Copy Score" in r for r in result.reasons)


@pytest.mark.django_db
def test_policy_rejects_over_max_trade(running_agent, source_trade, copy_relationship):
    perm = DreamAgentPermission.objects.get(agent=running_agent)
    result = PolicyEngine().evaluate(
        PolicyContext(
            agent=running_agent,
            permission=perm,
            source_trade=source_trade,
            relationship=copy_relationship,
            copy_score=95,
            amount=Decimal("25"),
            daily_volume=Decimal("0"),
        )
    )
    assert not result.ok
    assert any("max per trade" in r for r in result.reasons)


@pytest.mark.django_db
def test_policy_rejects_disallowed_trader(
    running_agent, source_trade, copy_relationship, trader
):
    perm = DreamAgentPermission.objects.get(agent=running_agent)
    perm.allowed_traders_json = ["99999"]
    perm.save(update_fields=["allowed_traders_json"])
    result = PolicyEngine().evaluate(
        PolicyContext(
            agent=running_agent,
            permission=perm,
            source_trade=source_trade,
            relationship=copy_relationship,
            copy_score=95,
            amount=Decimal("5"),
            daily_volume=Decimal("0"),
        )
    )
    assert not result.ok
    assert any("allowed list" in r for r in result.reasons)


@pytest.mark.django_db
def test_lifecycle_revoke_stops_agent(running_agent, user):
    revoked = smart_account_service.revoke_agent(user, agent_id=running_agent.pk)
    assert revoked.status == DreamAgent.Status.REVOKED
    perm = DreamAgentPermission.objects.filter(agent=revoked).first()
    assert perm.status == DreamAgentPermission.Status.REVOKED
    assert dream_agent_service.get_running_agent(user) is None
    assert dream_agent_service.get_tradable_agent(user) is None


@pytest.mark.django_db
def test_grant_health_flags_stale_caveat_args(running_agent, user, settings):
    settings.MOCK_SMART_ACCOUNT = False
    perm = DreamAgentPermission.objects.get(agent=running_agent)
    blob = dict(perm.signed_delegation_json or {})
    blob.pop("mock", None)
    blob["signature"] = "0x" + "ab" * 65
    blob["typed_data"] = {
        "primaryType": "Delegation",
        "types": {
            "Caveat": [
                {"name": "enforcer", "type": "address"},
                {"name": "terms", "type": "bytes"},
                {"name": "args", "type": "bytes"},
            ]
        },
    }
    perm.signed_delegation_json = blob
    perm.save(update_fields=["signed_delegation_json"])
    health = dream_agent_service.grant_health(user)
    assert health["needs_resign"] is True
    assert any("Caveat.args" in r for r in health["reasons"])


@pytest.mark.django_db
def test_grant_health_flags_grant_without_redeem(running_agent, user, settings):
    from eth_utils import function_signature_to_4byte_selector
    from integrations.metamask import PLACE_BINARY_ORDER_SELECTOR

    perm = DreamAgentPermission.objects.get(agent=running_agent)
    blob = dict(perm.signed_delegation_json or {})
    packed = (
        function_signature_to_4byte_selector(PLACE_BINARY_ORDER_SELECTOR)
        + function_signature_to_4byte_selector("approve(address,uint256)")
    )
    blob["caveats"] = [
        {
            "enforcer": settings.METAMASK_ALLOWED_METHODS_ENFORCER,
            "terms": "0x" + packed.hex(),
            "args": "0x",
        }
    ]
    perm.signed_delegation_json = blob
    perm.save(update_fields=["signed_delegation_json"])
    health = dream_agent_service.grant_health(user)
    assert health["needs_resign"] is True
    assert any("redeem" in r.lower() for r in health["reasons"])


@pytest.mark.django_db
def test_authorized_grant_is_tradable_without_autonomous_copy(
    user, smart_account, trader, sample_event, settings
):
    settings.MOCK_SMART_ACCOUNT = True
    smart_account_service.mark_funded(smart_account, amount=Decimal("50"))
    agent, perm = smart_account_service.grant_agent(
        user,
        max_trade_amount=Decimal("10"),
        max_daily_volume=Decimal("50"),
        expires_in_days=30,
        min_copy_score=50,
        allowed_traders=[str(trader.pk)],
        signed_delegation={
            "delegate": "0xSession00000000000000000000000000000001",
            "delegator": smart_account.address,
            "authority": "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            "caveats": [],
            "salt": "0x1",
            "signature": "0xmockdeadbeef",
            "mock": True,
        },
        activate=False,
    )
    assert agent.status == DreamAgent.Status.AUTHORIZED
    assert perm.status == DreamAgentPermission.Status.ACTIVE
    assert dream_agent_service.get_running_agent(user) is None
    assert dream_agent_service.get_tradable_agent(user) == agent

    with patch(
        "services.dream_agent_service.broadcast_delegated_execution",
        return_value="0x" + "ab" * 32,
    ):
        trade = dream_agent_service.execute_agent_manual_trade(
            user,
            event_id=sample_event.pk,
            outcome="YES",
            amount=Decimal("5"),
        )
    assert trade.transaction_hash == "0x" + "ab" * 32


@pytest.mark.django_db
def test_start_wizard_trade_step_when_granted(client, user, running_agent, sample_event):
    client.force_login(user)
    res = client.get("/start/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "Place a $1 YES or NO" in body
    assert "YES means you think this happens" in body
    assert 'data-amount="1"' in body


@pytest.mark.django_db
def test_web_agent_trade_api(client, user, running_agent, sample_event, settings):
    settings.MOCK_SMART_ACCOUNT = True
    client.force_login(user)
    with patch(
        "services.dream_agent_service.broadcast_delegated_execution",
        return_value="0x" + "cd" * 32,
    ):
        res = client.post(
            "/api/agent/trade/",
            data={"event_id": sample_event.pk, "outcome": "YES", "amount": "1"},
            content_type="application/json",
        )
    assert res.status_code == 201
    payload = res.json()
    assert payload["via_smart_account"] is True
    assert payload["tx_hash"] == "0x" + "cd" * 32
    assert payload["trade_id"]
    from apps.trading.models import Trade
    from services.onboarding_service import first_session_state

    trade = Trade.objects.get(pk=payload["trade_id"])
    meta = trade.metadata_json or {}
    assert meta.get("source") == "web"
    assert str(meta.get("smart_account") or "").lower() == running_agent.smart_account.address.lower()
    state = first_session_state(user)
    assert state["step"] == "done"
    assert state["incomplete"] is False
    assert state["has_first_trade"] is True


def test_grant_typed_data_is_delegation_manager_payload():
    from integrations.metamask.delegation import build_grant_typed_data

    typed = build_grant_typed_data(
        chain_id=50312,
        delegator="0x" + "11" * 20,
        delegate="0x" + "22" * 20,
        verifying_contract="0xf3f380e58d1742747338c46786cc7d5f9e71ef5c",
        max_trade_amount="10",
        max_daily_volume="50",
        expires_at=1_900_000_000,
        salt="0x1",
    )
    assert typed["primaryType"] == "Delegation"
    assert typed["domain"]["name"] == "DelegationManager"
    assert typed["domain"]["version"] == "1"
    assert typed["message"]["delegate"].startswith("0x")
    assert isinstance(typed["message"]["caveats"], list)
    caveat_names = [f["name"] for f in typed["types"]["Caveat"]]
    assert caveat_names == ["enforcer", "terms"]
    for caveat in typed["message"]["caveats"]:
        assert "args" not in caveat
        assert set(caveat) == {"enforcer", "terms"}
    methods = next(
        (
            c
            for c in typed["message"]["caveats"]
            if c["terms"].startswith("0x")
            and len(bytes.fromhex(c["terms"][2:])) not in (0, 32)
        ),
        None,
    )
    assert methods is not None
    raw = bytes.fromhex(methods["terms"][2:])
    assert len(raw) == 28  # seven 4-byte selectors
    from integrations.metamask.delegation import REDEEM_SELECTOR

    assert REDEEM_SELECTOR in {raw[i : i + 4] for i in range(0, len(raw), 4)}
    assert typed["dreamlens"]["permission"] == "TRADE_EVENT_CONTRACT"
    assert isinstance(typed["message"]["salt"], int)


@pytest.mark.django_db
def test_pause_and_resume(running_agent, user):
    paused = smart_account_service.set_agent_status(user, DreamAgent.Status.PAUSED)
    assert paused.status == DreamAgent.Status.PAUSED
    resumed = smart_account_service.set_agent_status(user, DreamAgent.Status.RUNNING)
    assert resumed.status == DreamAgent.Status.RUNNING


@pytest.mark.django_db
def test_invalid_lifecycle_transition(running_agent, user):
    with pytest.raises(SmartAccountError):
        smart_account_service.set_agent_status(user, DreamAgent.Status.CREATED)


@pytest.mark.django_db
def test_agent_autonomous_copy_creates_evaluation(
    running_agent,
    copy_relationship,
    source_trade,
    settings,
):
    settings.MOCK_SMART_ACCOUNT = True
    settings.MOCK_DREAMDEX = True
    # Lower score threshold / risk so COPY can succeed
    copy_relationship.auto_execute = True
    copy_relationship.min_copy_score = 1
    copy_relationship.min_win_rate = Decimal("0")
    copy_relationship.min_completed_events = 0
    copy_relationship.min_liquidity = Decimal("0")
    copy_relationship.save()
    perm = DreamAgentPermission.objects.get(agent=running_agent)
    perm.min_copy_score = 1
    perm.save(update_fields=["min_copy_score"])

    with patch("services.copy_score.evaluate_copy_score") as mock_score:
        from services.copy_score import CopyScoreResult

        mock_score.return_value = CopyScoreResult(
            decision="COPY",
            overall=87,
            confidence=Decimal("0.87"),
            pillars={"trader": 92, "event": 84, "consensus": 81},
            why=["Strong trader"],
            risks=[],
            skip_reasons=[],
            liquidity=Decimal("5000"),
        )
        results = detect_and_process_copy(source_trade)

    assert len(results) == 1
    execution = results[0]
    assert execution.status == CopyExecution.Status.EXECUTED
    ev = AgentEvaluation.objects.filter(agent=running_agent).first()
    assert ev is not None
    assert ev.decision == AgentEvaluation.Decision.COPY
    assert ev.tx_hash.startswith("0x")


@pytest.mark.django_db
def test_agent_skip_records_reasons(
    running_agent,
    copy_relationship,
    source_trade,
    settings,
):
    settings.MOCK_SMART_ACCOUNT = True
    copy_relationship.auto_execute = True
    copy_relationship.save(update_fields=["auto_execute"])
    perm = DreamAgentPermission.objects.get(agent=running_agent)
    perm.min_copy_score = 99
    perm.save(update_fields=["min_copy_score"])

    with patch("services.copy_score.evaluate_copy_score") as mock_score:
        from services.copy_score import CopyScoreResult

        mock_score.return_value = CopyScoreResult(
            decision="COPY",
            overall=63,
            confidence=Decimal("0.63"),
            pillars={"trader": 50, "event": 40, "consensus": 40},
            why=[],
            risks=["low liquidity"],
            skip_reasons=[],
            liquidity=Decimal("100"),
        )
        detect_and_process_copy(source_trade)

    ev = AgentEvaluation.objects.filter(
        agent=running_agent,
        decision=AgentEvaluation.Decision.SKIPPED,
    ).first()
    assert ev is not None
    assert any("Copy Score" in r or "minimum" in r for r in ev.skip_reasons_json)


@pytest.mark.django_db
def test_follow_after_grant_adds_trader_to_allowlist(running_agent, user, trader):
    from apps.dreamcopy.models import TraderProfile
    from services.copy_service import create_copy_relationship

    perm = DreamAgentPermission.objects.get(agent=running_agent)
    assert str(trader.pk) in {str(x) for x in perm.allowed_traders_json}

    other = TraderProfile.objects.create(
        wallet_address="0xBeta0000000000000000000000000000000000002",
        display_name="BetaTrader",
        completed_trades=20,
        trader_score=Decimal("0.70"),
        total_trades=22,
    )
    create_copy_relationship(user, {"trader_id": other.pk})
    perm.refresh_from_db()
    allowed = {str(x) for x in perm.allowed_traders_json}
    assert str(trader.pk) in allowed
    assert str(other.pk) in allowed


@pytest.mark.django_db
def test_expired_permission_blocks_policy(running_agent, source_trade, copy_relationship):
    perm = DreamAgentPermission.objects.get(agent=running_agent)
    perm.expires_at = timezone.now() - timedelta(hours=1)
    perm.save(update_fields=["expires_at"])
    # is_valid becomes False
    result = PolicyEngine().evaluate(
        PolicyContext(
            agent=running_agent,
            permission=perm,
            source_trade=source_trade,
            relationship=copy_relationship,
            copy_score=99,
            amount=Decimal("5"),
            daily_volume=Decimal("0"),
        )
    )
    assert not result.ok


@pytest.mark.django_db
def test_activate_page_offers_stt_gas_deposit(client, user, settings):
    settings.MOCK_SMART_ACCOUNT = True
    client.force_login(user)
    res = client.get("/agent/activate/")
    assert res.status_code == 200
    assert b"Add network fee" in res.content
    assert b"sa-deposit-gas" in res.content
    assert b"dl-agent-rail" in res.content
    assert b"id=\"sa-create\"" in res.content
    assert b"id=\"grant-permission\"" in res.content
    assert b"Create your trading account" in res.content
    assert b"Hybrid" not in res.content
    assert b"EIP-712" not in res.content
    assert b"Delegation Framework not configured" not in res.content


@pytest.mark.django_db
def test_agent_dashboard_shows_kpis_when_running(client, user, running_agent):
    client.force_login(user)
    res = client.get("/agent/")
    assert res.status_code == 200
    assert running_agent.name.encode() in res.content
    assert b"Active" in res.content
    assert b"dl-ta-kpis" in res.content
    assert b"data-agent-pause" in res.content
    assert b"data-agent-revoke" in res.content
    assert b"copies traders you follow" in res.content
    assert running_agent.smart_account.address[-4:].encode() in res.content


@pytest.mark.django_db
def test_agent_dashboard_empty_state_teaches_setup(client, user):
    client.force_login(user)
    res = client.get("/agent/")
    assert res.status_code == 200
    assert b"No Dream Agent yet" in res.content
    assert b"Activate Dream Agent" in res.content
    assert b"Create" in res.content
    assert b"Deposit" in res.content
    assert b"Grant" in res.content


@pytest.mark.django_db
def test_portfolio_shows_running_agent(client, user, running_agent):
    client.force_login(user)
    res = client.get("/portfolio/")
    assert res.status_code == 200
    assert running_agent.name.encode() in res.content
    assert b"Active" in res.content
    assert running_agent.smart_account.address[-4:].encode() in res.content
    assert b"Agent details" in res.content
    assert b"Agent gas" in res.content
    assert b"dl-ta-kpis" in res.content
    assert b"telegram-link-card" in res.content


@pytest.mark.django_db
def test_portfolio_prompts_to_activate_agent_when_missing(client, user):
    client.force_login(user)
    res = client.get("/portfolio/")
    assert res.status_code == 200
    assert b"Activate Dream Agent" in res.content
    assert b"No Dream Agent" in res.content


@pytest.mark.django_db
def test_grant_api_keeps_notify_me_follows(client, user, copy_relationship):
    copy_relationship.auto_execute = False
    copy_relationship.save(update_fields=["auto_execute"])
    client.force_login(user)
    perm = type(
        "Perm",
        (),
        {
            "pk": 1,
            "status": "ACTIVE",
            "max_trade_amount": Decimal("10"),
            "max_daily_volume": Decimal("50"),
            "min_copy_score": 75,
            "expires_at": timezone.now(),
        },
    )()
    agent = type(
        "Agent",
        (),
        {"pk": 1, "name": "DreamAgent", "status": "RUNNING", "session_address": "0x1"},
    )()
    with patch("apps.core.api.agents.smart_account_service.grant_agent", return_value=(agent, perm)):
        res = client.post(
            "/api/agent/grant/",
            data={"activate": True, "signed_delegation": {"mock": True}},
            content_type="application/json",
        )
    assert res.status_code == 201
    copy_relationship.refresh_from_db()
    assert copy_relationship.auto_execute is False
    assert copy_relationship.max_per_trade == Decimal("10")
