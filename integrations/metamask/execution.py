"""Build execution payloads for delegated DreamDEX trades."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from integrations.dreamdex.types import UnsignedTxDTO
from integrations.metamask.delegation import encode_redeem_delegations_calldata
from integrations.metamask.smart_account import (
    SmartAccountConfigError,
    mock_smart_account_enabled,
    require_live_environment,
)

# 0.05 STT default cap if settings are unavailable (tests).
_DEFAULT_GAS_PAYMENT_CAP_WEI = 5 * 10**16


@dataclass
class DelegatedExecution:
    """Ready-to-broadcast redeem tx (from session EOA)."""

    to: str
    data: str
    value: int
    chain_id: int
    mock: bool
    inner_target: str
    inner_data: str
    inner_value: int = 0
    signed_delegation: dict[str, Any] | None = None
    gas_payment_wei: int = 0
    pre_executions: list[tuple[str, int, str]] = field(default_factory=list)


def compute_gas_reimbursement_wei(
    *,
    sa_balance_wei: int,
    gas_limit: int,
    gas_price_wei: int,
    buffer_bps: int = 2500,
    cap_wei: int = _DEFAULT_GAS_PAYMENT_CAP_WEI,
) -> int:
    """Native STT the Smart Account should send the session key for this redeem."""
    if sa_balance_wei <= 0 or gas_limit <= 0 or gas_price_wei <= 0:
        return 0
    bps = max(0, int(buffer_bps))
    payment = int(gas_limit) * int(gas_price_wei) * (10_000 + bps) // 10_000
    if cap_wei > 0:
        payment = min(payment, int(cap_wei))
    return min(payment, int(sa_balance_wei))


def with_gas_reimbursement(
    execution: DelegatedExecution,
    *,
    recipient: str,
    amount_wei: int,
) -> DelegatedExecution:
    """Append a native STT transfer from the Smart Account to the session EOA."""
    if execution.mock or not execution.signed_delegation or amount_wei <= 0:
        return execution
    data = encode_redeem_delegations_calldata(
        signed_delegation=execution.signed_delegation,
        target=execution.inner_target,
        call_data=execution.inner_data,
        value=execution.inner_value,
        pre_executions=list(execution.pre_executions or []),
        extra_executions=[(recipient, int(amount_wei), "0x")],
    )
    return replace(execution, data=data, gas_payment_wei=int(amount_wei))


def build_delegated_trade_execution(
    *,
    signed_delegation: dict[str, Any],
    dreamdex_tx: UnsignedTxDTO,
    chain_id: int | None = None,
    approval_tx: UnsignedTxDTO | None = None,
) -> DelegatedExecution:
    """Wrap a DreamDEX placeBinaryOrder UnsignedTxDTO in redeemDelegations."""
    cid = chain_id or dreamdex_tx.chain_id
    pre: list[tuple[str, int, str]] = []
    if approval_tx and approval_tx.to and approval_tx.data:
        pre.append(
            (
                approval_tx.to,
                int(approval_tx.value or 0),
                approval_tx.data,
            )
        )
    if mock_smart_account_enabled():
        return DelegatedExecution(
            to=dreamdex_tx.to,
            data=dreamdex_tx.data,
            value=int(dreamdex_tx.value or 0),
            chain_id=dreamdex_tx.chain_id,
            mock=True,
            inner_target=dreamdex_tx.to,
            inner_data=dreamdex_tx.data,
            inner_value=int(dreamdex_tx.value or 0),
            signed_delegation=signed_delegation,
            pre_executions=pre,
        )

    env = require_live_environment(chain_id=cid)
    if not env.delegation_manager:
        raise SmartAccountConfigError("DelegationManager address missing")

    data = encode_redeem_delegations_calldata(
        signed_delegation=signed_delegation,
        target=dreamdex_tx.to,
        call_data=dreamdex_tx.data,
        value=int(dreamdex_tx.value or 0),
        pre_executions=pre,
    )
    return DelegatedExecution(
        to=env.delegation_manager,
        data=data,
        value=0,
        chain_id=env.chain_id,
        mock=False,
        inner_target=dreamdex_tx.to,
        inner_data=dreamdex_tx.data,
        inner_value=int(dreamdex_tx.value or 0),
        signed_delegation=signed_delegation,
        pre_executions=pre,
    )


def wrap_owner_execute(smart_account: str, inner: UnsignedTxDTO) -> UnsignedTxDTO:
    """Owner-signed HybridDeleGator.execute wrapping an inner DreamDEX call.

    Outcome tokens from Telegram / DreamAgent fills sit on the Smart Account.
    MetaMask only signs as the owner EOA, so claim/close must go
    owner → SA.execute(inner) rather than asking the user to switch to the SA.
    """
    from eth_abi import encode
    from eth_utils import function_signature_to_4byte_selector, to_checksum_address

    from integrations.metamask import EXECUTION_MODE_SINGLE_DEFAULT
    from integrations.metamask.delegation import encode_single_execution_packed

    sa = to_checksum_address(smart_account)
    mode = bytes.fromhex(EXECUTION_MODE_SINGLE_DEFAULT[2:])
    packed = encode_single_execution_packed(
        inner.to, inner.data or "0x", value=int(inner.value or 0)
    )
    selector = function_signature_to_4byte_selector("execute(bytes32,bytes)")
    data = "0x" + (selector + encode(["bytes32", "bytes"], [mode, packed])).hex()
    meta = dict(inner.metadata or {})
    meta["via_smart_account"] = sa
    meta["inner_to"] = inner.to
    return UnsignedTxDTO(
        to=sa,
        data=data,
        value=0,
        chain_id=inner.chain_id,
        description=inner.description,
        metadata=meta,
    )
