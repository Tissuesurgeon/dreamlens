"""Broadcast delegated executions from the DreamAgent session key."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from eth_account import Account
from web3 import Web3

from integrations.metamask.execution import (
    DelegatedExecution,
    compute_gas_reimbursement_wei,
    with_gas_reimbursement,
)
from integrations.metamask.smart_account import mock_smart_account_enabled

logger = logging.getLogger("dreamlens.metamask.transactions")


class SessionKeyError(Exception):
    pass


def session_key_configured() -> bool:
    return bool(getattr(settings, "DREAM_AGENT_SESSION_KEY", "") or "")


def get_session_address() -> str:
    """Public address of the backend session EOA."""
    key = getattr(settings, "DREAM_AGENT_SESSION_KEY", "") or ""
    if key:
        if not str(key).startswith("0x"):
            key = "0x" + key
        return Account.from_key(key).address
    if mock_smart_account_enabled():
        return Web3.to_checksum_address("0x" + "11" * 20)
    raise SessionKeyError(
        "DREAM_AGENT_SESSION_KEY is not set. Generate a session EOA for redeemDelegations."
    )


def wait_for_receipt(tx_hash: str, *, timeout: int = 120):
    rpc = getattr(settings, "DREAMDEX_RPC_URL", "")
    w3 = Web3(Web3.HTTPProvider(rpc))
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)


def get_transaction_receipt(tx_hash: str):
    rpc = getattr(settings, "DREAMDEX_RPC_URL", "")
    w3 = Web3(Web3.HTTPProvider(rpc))
    return w3.eth.get_transaction_receipt(tx_hash)


def apply_smart_account_gas_payment(
    execution: DelegatedExecution,
    *,
    session_address: str,
    w3,
) -> DelegatedExecution:
    """Have the Hybrid Smart Account repay session-key gas in the same redeem."""
    if execution.mock or not getattr(settings, "DREAM_AGENT_SA_PAYS_GAS", True):
        return execution
    sa = (execution.signed_delegation or {}).get("delegator") or ""
    if not sa:
        return execution
    try:
        sa_bal = int(w3.eth.get_balance(Web3.to_checksum_address(sa)))
        gas_price = int(w3.eth.gas_price)
        payment = compute_gas_reimbursement_wei(
            sa_balance_wei=sa_bal,
            gas_limit=int(getattr(settings, "DREAM_AGENT_GAS_LIMIT", 800_000)),
            gas_price_wei=gas_price,
            buffer_bps=int(getattr(settings, "DREAM_AGENT_GAS_BUFFER_BPS", 2500)),
            cap_wei=int(
                getattr(settings, "DREAM_AGENT_MAX_GAS_PAYMENT_WEI", 5 * 10**16)
            ),
        )
        if payment <= 0:
            logger.warning(
                "sa_pays_gas skipped sa=%s balance_wei=%s", sa, sa_bal
            )
            return execution
        updated = with_gas_reimbursement(
            execution, recipient=session_address, amount_wei=payment
        )
        logger.info(
            "sa_pays_gas sa=%s reimbursement_wei=%s session=%s",
            sa,
            payment,
            session_address,
        )
        return updated
    except Exception as exc:  # noqa: BLE001
        logger.warning("sa_pays_gas failed: %s", exc)
        return execution


def broadcast_delegated_execution(
    execution: DelegatedExecution,
    *,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Sign and send redeem tx. Never uses the user's owner key."""
    if execution.mock:
        if not mock_smart_account_enabled():
            raise SessionKeyError("Refusing mock broadcast on live Somnia")
        # Tests only — never used at runtime when MOCK_SMART_ACCOUNT=false
        from eth_utils import keccak

        digest = keccak(
            text=f"test-delegated:{execution.inner_target}:{execution.inner_data}"
        )
        tx_hash = "0x" + digest.hex()
        logger.info("test_delegated_execution hash=%s", tx_hash)
        return tx_hash

    key = getattr(settings, "DREAM_AGENT_SESSION_KEY", "") or ""
    if not key:
        raise SessionKeyError(
            "DREAM_AGENT_SESSION_KEY not set — cannot redeem live delegations"
        )
    if not str(key).startswith("0x"):
        key = "0x" + key

    rpc = getattr(settings, "DREAMDEX_RPC_URL", "")
    w3 = Web3(Web3.HTTPProvider(rpc))
    acct = Account.from_key(key)
    execution = apply_smart_account_gas_payment(
        execution, session_address=acct.address, w3=w3
    )
    nonce = w3.eth.get_transaction_count(acct.address)
    tx = {
        "to": Web3.to_checksum_address(execution.to),
        "data": execution.data,
        "value": execution.value,
        "chainId": execution.chain_id,
        "nonce": nonce,
        "gas": int(getattr(settings, "DREAM_AGENT_GAS_LIMIT", 800_000)),
        "maxFeePerGas": w3.eth.gas_price,
        "maxPriorityFeePerGas": w3.eth.gas_price,
    }
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    hex_hash = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    if not hex_hash.startswith("0x"):
        hex_hash = "0x" + hex_hash
    logger.info(
        "delegated_execution broadcast hash=%s meta=%s gas_payment_wei=%s",
        hex_hash,
        metadata or {},
        execution.gas_payment_wei,
    )
    return hex_hash
