"""SmartAccountService — create, fund intent, grant/revoke DreamAgent, balances."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.agents.models import DreamAgent, DreamAgentPermission, SmartAccount
from integrations.metamask.delegation import (
    build_grant_typed_data,
    hash_delegation_blob,
    validate_signed_delegation,
)
from integrations.metamask.permissions import DreamAgentPermissionSpec
from integrations.metamask.smart_account import (
    SmartAccountConfigError,
    collateral_token_address,
    derive_counterfactual_address,
    get_environment,
    mock_smart_account_enabled,
    normalize_address,
    probe_hybrid_account,
    require_live_environment,
)
from integrations.metamask.transactions import (
    SessionKeyError,
    get_session_address,
    get_transaction_receipt,
)

logger = logging.getLogger("dreamlens.services.smart_account")


class SmartAccountError(Exception):
    pass


def get_account(user, *, chain_id: int | None = None) -> SmartAccount | None:
    cid = chain_id or int(getattr(settings, "DREAMDEX_CHAIN_ID", 50312))
    return (
        SmartAccount.objects.filter(user=user, chain_id=cid)
        .order_by("-updated_at")
        .first()
    )


@transaction.atomic
def create_account(
    user,
    *,
    owner_address: str,
    address: str | None = None,
    chain_id: int | None = None,
    factory_address: str = "",
    deploy_salt: str = "0x",
    metadata: dict[str, Any] | None = None,
) -> SmartAccount:
    cid = chain_id or int(getattr(settings, "DREAMDEX_CHAIN_ID", 50312))
    owner = normalize_address(owner_address)
    if mock_smart_account_enabled():
        env = get_environment(chain_id=cid)
        sa_address = address or derive_counterfactual_address(owner, salt=deploy_salt)
    else:
        env = require_live_environment(chain_id=cid)
        if not address:
            raise SmartAccountError(
                "Live Smart Account requires the Hybrid account address from MetaMask / factory"
            )
        sa_address = address
    sa_address = normalize_address(sa_address)

    existing = get_account(user, chain_id=cid)
    if existing:
        existing.owner_address = owner
        existing.address = sa_address
        existing.factory_address = factory_address or env.simple_factory
        existing.deploy_salt = deploy_salt
        if existing.status == SmartAccount.Status.PENDING:
            existing.status = SmartAccount.Status.DEPLOYED
        if metadata:
            existing.metadata_json = {**(existing.metadata_json or {}), **metadata}
        existing.save()
        return existing

    return SmartAccount.objects.create(
        user=user,
        owner_address=owner,
        address=sa_address,
        chain_id=cid,
        factory_address=factory_address or env.simple_factory,
        deploy_salt=deploy_salt,
        status=SmartAccount.Status.DEPLOYED,
        metadata_json={
            **(metadata or {}),
        },
    )


@transaction.atomic
def mark_funded(smart_account: SmartAccount, *, amount: Decimal | None = None) -> SmartAccount:
    smart_account.status = SmartAccount.Status.FUNDED
    meta = dict(smart_account.metadata_json or {})
    if amount is not None:
        meta["last_deposit"] = str(amount)
    smart_account.metadata_json = meta
    smart_account.save(update_fields=["status", "metadata_json", "updated_at"])

    for agent in smart_account.agents.exclude(
        status__in=[DreamAgent.Status.REVOKED, DreamAgent.Status.RUNNING]
    ):
        if agent.status == DreamAgent.Status.CREATED and agent.can_transition_to(
            DreamAgent.Status.FUNDED
        ):
            agent.status = DreamAgent.Status.FUNDED
            if amount is not None and agent.initial_capital <= 0:
                agent.initial_capital = amount
            agent.save(update_fields=["status", "initial_capital", "updated_at"])
    return smart_account


@transaction.atomic
def ensure_agent(
    user,
    smart_account: SmartAccount,
    *,
    name: str = "DreamAgent",
) -> DreamAgent:
    agent = (
        DreamAgent.objects.filter(user=user, smart_account=smart_account)
        .exclude(status=DreamAgent.Status.REVOKED)
        .order_by("-updated_at")
        .first()
    )
    try:
        session = get_session_address()
    except SessionKeyError as exc:
        raise SmartAccountError(str(exc)) from exc
    if agent:
        if agent.session_address.lower() != session.lower():
            agent.session_address = session
            agent.save(update_fields=["session_address", "updated_at"])
        return agent
    return DreamAgent.objects.create(
        user=user,
        smart_account=smart_account,
        name=name,
        session_address=session,
        status=DreamAgent.Status.CREATED,
    )


@transaction.atomic
def grant_agent(
    user,
    *,
    max_trade_amount: Decimal = Decimal("10"),
    max_daily_volume: Decimal = Decimal("50"),
    expires_in_days: int = 30,
    min_copy_score: int = 75,
    allowed_traders: list | None = None,
    allowed_outcomes: list | None = None,
    allowed_contracts: list | None = None,
    signed_delegation: dict | None = None,
    activate: bool = True,
) -> tuple[DreamAgent, DreamAgentPermission]:
    sa = get_account(user)
    if not sa:
        raise SmartAccountError("Create a DreamLens Smart Account first")
    if not mock_smart_account_enabled():
        require_live_environment(chain_id=sa.chain_id)

    agent = ensure_agent(user, sa)
    if sa.status == SmartAccount.Status.FUNDED and agent.status == DreamAgent.Status.CREATED:
        agent.status = DreamAgent.Status.FUNDED
        agent.save(update_fields=["status", "updated_at"])

    if agent.status in (
        DreamAgent.Status.CREATED,
        DreamAgent.Status.FUNDED,
    ):
        agent.status = DreamAgent.Status.CONFIGURED
        agent.save(update_fields=["status", "updated_at"])

    expires_at = timezone.now() + timedelta(days=int(expires_in_days))
    spec = DreamAgentPermissionSpec(
        max_trade_amount=Decimal(max_trade_amount),
        max_daily_volume=Decimal(max_daily_volume),
        expires_at=expires_at,
        min_copy_score=int(min_copy_score),
        allowed_traders=[str(x) for x in (allowed_traders or [])],
        allowed_outcomes=list(allowed_outcomes or []),
        allowed_contracts=[normalize_address(c) for c in (allowed_contracts or []) if c],
    )

    if mock_smart_account_enabled():
        delegation = signed_delegation or {
            "delegate": agent.session_address,
            "delegator": sa.address,
            "authority": "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            "caveats": [],
            "salt": "0x0",
            "signature": "0x" + "ab" * 65,
            "mock": True,
        }
    else:
        if not signed_delegation:
            raise SmartAccountError("signed_delegation from MetaMask is required")
        delegation = signed_delegation
        if delegation.get("mock") or str(delegation.get("signature", "")).startswith("0xmock"):
            raise SmartAccountError("Mock delegations are not accepted on live Somnia")
    ok, reasons = validate_signed_delegation(delegation)
    if not ok:
        raise SmartAccountError("; ".join(reasons))

    # Revoke previous active permissions for this agent
    DreamAgentPermission.objects.filter(
        agent=agent,
        status=DreamAgentPermission.Status.ACTIVE,
    ).update(
        status=DreamAgentPermission.Status.REVOKED,
        revoked_at=timezone.now(),
    )

    perm = DreamAgentPermission.objects.create(
        owner_address=sa.owner_address,
        agent=agent,
        smart_account=sa,
        chain_id=sa.chain_id,
        max_trade_amount=spec.max_trade_amount,
        max_daily_volume=spec.max_daily_volume,
        min_copy_score=spec.min_copy_score,
        allowed_traders_json=spec.allowed_traders,
        allowed_outcomes_json=spec.allowed_outcomes,
        allowed_contracts_json=spec.allowed_contracts,
        caveats_json=spec.to_caveats_json(),
        signed_delegation_json=delegation,
        delegation_hash=hash_delegation_blob(delegation),
        expires_at=expires_at,
        status=DreamAgentPermission.Status.ACTIVE,
    )

    agent.status = DreamAgent.Status.AUTHORIZED
    if activate:
        agent.status = DreamAgent.Status.RUNNING
    agent.save(update_fields=["status", "updated_at"])
    logger.info("grant_agent user=%s agent=%s perm=%s", user.pk, agent.pk, perm.pk)
    return agent, perm


@transaction.atomic
def revoke_agent(user, *, agent_id: int | None = None) -> DreamAgent:
    qs = DreamAgent.objects.filter(user=user).exclude(status=DreamAgent.Status.REVOKED)
    if agent_id:
        qs = qs.filter(pk=agent_id)
    agent = qs.order_by("-updated_at").first()
    if not agent:
        raise SmartAccountError("No active DreamAgent to revoke")

    DreamAgentPermission.objects.filter(
        agent=agent,
        status=DreamAgentPermission.Status.ACTIVE,
    ).update(
        status=DreamAgentPermission.Status.REVOKED,
        revoked_at=timezone.now(),
    )
    agent.status = DreamAgent.Status.REVOKED
    agent.save(update_fields=["status", "updated_at"])
    logger.info("revoke_agent user=%s agent=%s", user.pk, agent.pk)
    return agent


@transaction.atomic
def set_agent_status(user, status: str, *, agent_id: int | None = None) -> DreamAgent:
    qs = DreamAgent.objects.filter(user=user).exclude(status=DreamAgent.Status.REVOKED)
    if agent_id:
        qs = qs.filter(pk=agent_id)
    agent = qs.order_by("-updated_at").first()
    if not agent:
        raise SmartAccountError("DreamAgent not found")
    if status == DreamAgent.Status.REVOKED:
        return revoke_agent(user, agent_id=agent.pk)
    if not agent.can_transition_to(status):
        raise SmartAccountError(f"Cannot transition {agent.status} → {status}")
    agent.status = status
    agent.save(update_fields=["status", "updated_at"])
    return agent


def verify_deposit_tx(*, tx_hash: str, smart_account: SmartAccount) -> dict[str, Any]:
    """Require a successful on-chain receipt before marking FUNDED."""
    if mock_smart_account_enabled():
        return {"status": 1, "tx_hash": tx_hash, "mock": True}
    raw = (tx_hash or "").strip()
    if not raw.startswith("0x") or len(raw) != 66 or raw.lower().startswith("0xmock"):
        raise SmartAccountError("A real Somnia transaction hash is required")
    try:
        receipt = get_transaction_receipt(raw)
    except Exception as exc:  # noqa: BLE001
        raise SmartAccountError(f"Could not fetch deposit receipt: {exc}") from exc
    status = int(receipt.get("status", 0) if isinstance(receipt, dict) else receipt.status)
    if status != 1:
        raise SmartAccountError("Deposit transaction failed on-chain")
    return {"status": status, "tx_hash": raw, "mock": False}


def get_balance(smart_account: SmartAccount) -> dict[str, Any]:
    """Return balance snapshot from RPC when live."""
    meta = smart_account.metadata_json or {}
    if mock_smart_account_enabled():
        return {
            "address": smart_account.address,
            "mock": True,
            "collateral": meta.get("balance", meta.get("last_deposit", "0")),
            "native": meta.get("native", "0"),
            "native_symbol": "STT",
            "collateral_symbol": "USDC",
        }
    try:
        from integrations.dreamdex.adapter import get_adapter

        adapter = get_adapter()
        getter = getattr(adapter, "get_wallet_balances", None) or getattr(
            adapter, "get_balances", None
        )
        if getter:
            balances = getter(smart_account.address)
            if isinstance(balances, dict):
                payload = {
                    "native": str(
                        balances.get("native")
                        or balances.get("native_balance")
                        or "0"
                    ),
                    "collateral": str(
                        balances.get("collateral")
                        or balances.get("collateral_balance")
                        or balances.get("usdc")
                        or "0"
                    ),
                    "native_symbol": balances.get("native_symbol", "STT"),
                    "collateral_symbol": balances.get("collateral_symbol", "USDC"),
                }
            elif hasattr(balances, "native_balance") or hasattr(balances, "native"):
                payload = {
                    "native": str(
                        getattr(balances, "native", None)
                        or getattr(balances, "native_balance", "0")
                    ),
                    "collateral": str(
                        getattr(balances, "collateral", None)
                        or getattr(balances, "collateral_balance", None)
                        or getattr(balances, "usdc", "0")
                    ),
                    "native_symbol": getattr(balances, "native_symbol", "STT"),
                    "collateral_symbol": getattr(
                        balances, "collateral_symbol", "USDC"
                    ),
                }
            else:
                payload = {"raw": str(balances)}
            return {"address": smart_account.address, "mock": False, **payload}
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_balance failed: %s", exc)
        raise SmartAccountError(f"Could not read on-chain balance: {exc}") from exc
    raise SmartAccountError("Balance adapter unavailable")


def grant_payload_for_ui(user, *, owner_address: str = "") -> dict[str, Any]:
    sa = get_account(user)
    agent = None
    session = ""
    configured = True
    config_error = ""
    deploy_error = ""
    try:
        if not mock_smart_account_enabled():
            require_live_environment()
        session = get_session_address()
    except (SmartAccountConfigError, SessionKeyError) as exc:
        configured = False
        config_error = str(exc)
    if sa and configured:
        try:
            agent = ensure_agent(user, sa)
        except SmartAccountError as exc:
            configured = False
            config_error = str(exc)
    spec = DreamAgentPermissionSpec()
    env = get_environment()
    expires_at = int((timezone.now() + timedelta(days=30)).timestamp())
    salt = "0x" + timezone.now().strftime("%Y%m%d%H%M%S").ljust(64, "0")[:64]
    deploy_tx = None
    owner = (owner_address or (sa.owner_address if sa else "") or "").strip()
    if not owner and user is not None:
        from apps.accounts.models import Wallet

        linked = (
            Wallet.objects.filter(user=user, is_primary=True).first()
            or Wallet.objects.filter(user=user).first()
        )
        if linked:
            owner = linked.address
    if configured and env.simple_factory and env.hybrid_implementation and owner:
        from integrations.metamask.smart_account import encode_factory_deploy_tx, owner_deploy_salt

        deploy_salt = owner_deploy_salt(owner)
        try:
            deploy_tx = encode_factory_deploy_tx(
                implementation=env.hybrid_implementation,
                salt=deploy_salt,
                owner_address=owner,
            )
            probe = probe_hybrid_account(deploy_tx["predicted_address"])
            deployed = probe["code_size"] > 0
            owner_ok = bool(probe["owner"] and probe["owner"].lower() == owner.lower())
            deploy_tx["already_deployed"] = bool(deployed and (owner_ok or not probe["owner"]))
            if deployed and probe["owner"] and not owner_ok:
                deploy_error = (
                    "A Smart Account already exists at this address for a different owner."
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to encode Hybrid Smart Account deploy tx")
            deploy_error = str(exc)
    typed_data = None
    if sa and session and env.delegation_manager:
        typed_data = build_grant_typed_data(
            chain_id=sa.chain_id,
            delegator=sa.address,
            delegate=session,
            verifying_contract=env.delegation_manager,
            max_trade_amount="10",
            max_daily_volume="50",
            expires_at=expires_at,
            salt=salt,
        )
    return {
        "smart_account": (
            {
                "id": sa.pk,
                "address": sa.address,
                "owner_address": sa.owner_address,
                "status": sa.status,
                "chain_id": sa.chain_id,
            }
            if sa
            else None
        ),
        "agent": (
            {
                "id": agent.pk,
                "name": agent.name,
                "session_address": agent.session_address,
                "status": agent.status,
            }
            if agent
            else None
        ),
        "session_address": session,
        "mock": mock_smart_account_enabled(),
        "configured": configured,
        "config_error": config_error,
        "deploy_error": deploy_error,
        "framework": {
            "delegation_manager": env.delegation_manager,
            "simple_factory": env.simple_factory,
            "hybrid_implementation": env.hybrid_implementation,
            "chain_id": env.chain_id,
            "collateral": collateral_token_address(),
        },
        "typed_data": typed_data,
        "deploy_tx": deploy_tx,
        "grant": spec.to_browser_grant_payload(),
        "agent_can": spec.agent_can(),
        "agent_cannot": spec.agent_cannot(),
    }
