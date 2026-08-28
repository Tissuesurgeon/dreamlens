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
    assert b"Deposit STT for gas" in res.content
    assert b"Pay Shannon gas" in res.content
    assert b"sa-deposit-gas" in res.content
    assert b"dl-agent-rail" in res.content
    assert b"id=\"sa-create\"" in res.content
    assert b"id=\"grant-permission\"" in res.content


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
