"""Broadcast delegated executions from the DreamAgent session key."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from eth_utils import to_checksum_address

from integrations.metamask.execution import (
    DelegatedExecution,
    compute_gas_reimbursement_wei,
    with_gas_reimbursement,
)
from integrations.metamask.smart_account import mock_smart_account_enabled

logger = logging.getLogger("dreamlens.metamask.transactions")


class SessionKeyError(Exception):
    pass


def _account():
    from eth_account import Account

    return Account


def _web3():
    from web3 import Web3

    return Web3


def session_key_configured() -> bool:
    return bool(getattr(settings, "DREAM_AGENT_SESSION_KEY", "") or "")


def get_session_address() -> str:
    """Public address of the backend session EOA."""
    key = getattr(settings, "DREAM_AGENT_SESSION_KEY", "") or ""
    if key:
        if not str(key).startswith("0x"):
            key = "0x" + key
        return _account().from_key(key).address
    if mock_smart_account_enabled():
        return to_checksum_address("0x" + "11" * 20)
    raise SessionKeyError(
        "DREAM_AGENT_SESSION_KEY is not set. Generate a session EOA for redeemDelegations."
    )


def wait_for_receipt(tx_hash: str, *, timeout: int = 120):
    rpc = getattr(settings, "DREAMDEX_RPC_URL", "")
    Web3 = _web3()
    w3 = Web3(Web3.HTTPProvider(rpc))
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)


def get_transaction_receipt(tx_hash: str):
    rpc = getattr(settings, "DREAMDEX_RPC_URL", "")
    Web3 = _web3()
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
    # FunctionCall caveats (AllowedMethods + ValueLte(0)) reject a native STT
    # transfer in the same redeem. Session EOA pays gas instead.
    if (execution.signed_delegation or {}).get("caveats"):
        logger.info("sa_pays_gas skipped: grant caveats require SingleDefault calls")
        return execution
    sa = (execution.signed_delegation or {}).get("delegator") or ""
    if not sa:
        return execution
    try:
        sa_bal = int(w3.eth.get_balance(to_checksum_address(sa)))
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
    Web3 = _web3()
    w3 = Web3(Web3.HTTPProvider(rpc))
    acct = _account().from_key(key)
    delegate = str((execution.signed_delegation or {}).get("delegate") or "")
    if delegate and delegate.lower() != acct.address.lower():
        raise SessionKeyError(
            "This grant was signed to a different session key. "
            "Re-sign DreamAgent at /agent/activate/."
        )
    execution = apply_smart_account_gas_payment(
        execution, session_address=acct.address, w3=w3
    )
    nonce = w3.eth.get_transaction_count(acct.address)
    steps = max(1, len((execution.pre_executions or [])) + 1)
    floor = int(getattr(settings, "DREAM_AGENT_GAS_LIMIT", 2_000_000)) * steps
    gas_price = int(w3.eth.gas_price)
    call_tx = {
        "from": acct.address,
        "to": to_checksum_address(execution.to),
        "data": execution.data,
        "value": execution.value,
    }
    try:
        estimated = int(w3.eth.estimate_gas(call_tx))
        gas_limit = max(floor, estimated + estimated // 4)
    except Exception as exc:  # noqa: BLE001
        raise SessionKeyError(_humanize_redeem_revert(exc)) from exc
    balance = int(w3.eth.get_balance(acct.address))
    need = gas_limit * gas_price
    if balance < need:
        raise SessionKeyError(
            f"DreamAgent session key {acct.address} needs Shannon STT for gas "
            f"(has {balance} wei, needs ~{need}). Fund that address — not your MetaMask."
        )
    tx = legacy_session_tx(
        to=execution.to,
        data=execution.data,
        value=execution.value,
        chain_id=execution.chain_id,
        nonce=nonce,
        gas=gas_limit,
        gas_price=gas_price,
    )
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    try:
        tx_hash = w3.eth.send_raw_transaction(raw)
    except Exception as exc:  # noqa: BLE001
        raise SessionKeyError(_humanize_redeem_revert(exc)) from exc
    hex_hash = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    if not hex_hash.startswith("0x"):
        hex_hash = "0x" + hex_hash
    logger.info(
        "delegated_execution submitted hash=%s gas=%s meta=%s",
        hex_hash,
        gas_limit,
        metadata or {},
    )
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    status = getattr(receipt, "status", None)
    if status is not None and int(status) != 1:
        raise SessionKeyError(
            _explain_failed_redeem(w3, hex_hash, gas_limit=gas_limit)
        )
    logger.info(
        "delegated_execution broadcast hash=%s meta=%s gas_payment_wei=%s",
        hex_hash,
        metadata or {},
        execution.gas_payment_wei,
    )
    return hex_hash


def legacy_session_tx(
    *,
    to: str,
    data: str,
    value: int,
    chain_id: int,
    nonce: int,
    gas: int,
    gas_price: int,
) -> dict[str, Any]:
    """Somnia Shannon rejects EIP-1559 (type 2) session txs with account does not exist."""
    return {
        "to": to_checksum_address(to),
        "data": data,
        "value": int(value),
        "chainId": int(chain_id),
        "nonce": int(nonce),
        "gas": int(gas),
        "gasPrice": int(gas_price),
    }


def _explain_failed_redeem(w3, tx_hash: str, *, gas_limit: int) -> str:
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        used = int(getattr(receipt, "gasUsed", 0) or 0)
        if gas_limit and used >= int(gas_limit) * 95 // 100:
            return (
                "DreamAgent redeem ran out of gas on Shannon. "
                "Retry — the next attempt uses a higher gas limit."
            )
    except Exception:  # noqa: BLE001
        receipt = None
    try:
        from integrations.dreamdex.client import explain_reverted_tx

        detail = explain_reverted_tx(tx_hash)
        if detail and "On-chain transaction failed" not in detail:
            return detail
    except Exception as exc:  # noqa: BLE001
        mapped = _humanize_redeem_revert(exc)
        if mapped:
            return mapped
    return (
        "DreamAgent redeem reverted on Shannon. Re-sign the grant at "
        "/agent/activate/ if this grant predates the DelegationManager fix."
    )


def _revert_text_blobs(exc: Exception) -> list[str]:
    blobs = [str(exc)]
    for arg in getattr(exc, "args", ()) or ():
        blobs.append(str(arg))
        hex_fn = getattr(arg, "hex", None)
        if callable(hex_fn):
            try:
                blobs.append(str(hex_fn()))
            except Exception:  # noqa: BLE001
                pass
    data = getattr(exc, "data", None)
    if data is not None:
        blobs.append(str(data))
        hex_fn = getattr(data, "hex", None)
        if callable(hex_fn):
            try:
                blobs.append(str(hex_fn()))
            except Exception:  # noqa: BLE001
                pass
    return blobs


def _decode_solidity_error_string(exc: Exception) -> str:
    """Turn Error(string) hex (0x08c379a0…) into the revert reason."""
    import re

    from eth_abi import decode

    joined = " ".join(_revert_text_blobs(exc))
    match = re.search(r"08c379a0([0-9a-fA-F]*)", joined, re.IGNORECASE)
    if match:
        hex_body = match.group(0)
        if len(hex_body) % 2:
            hex_body = hex_body[:-1]
        try:
            payload = bytes.fromhex(hex_body)
            (message,) = decode(["string"], payload[4:])
            if message:
                return message
        except Exception:  # noqa: BLE001
            pass
        try:
            ascii_blob = bytes.fromhex(hex_body)
            printable = "".join(chr(b) if 32 <= b < 127 else " " for b in ascii_blob)
            collapsed = " ".join(printable.split())
            if collapsed:
                return collapsed
        except Exception:  # noqa: BLE001
            pass
    return ""


def _humanize_redeem_revert(exc: Exception) -> str:
    from integrations.metamask.delegation import GRANT_MISSING_REDEEM

    raw = str(exc)
    decoded = _decode_solidity_error_string(exc)
    lowered = f"{raw} {decoded}".lower()
    if "account does not exist" in lowered or "'0x02'" in raw or '"0x02"' in raw:
        return (
            "Shannon rejected a type-2 session transaction. DreamAgent now sends a "
            "legacy tx — retry. If it still fails, fund the session EOA with STT."
        )
    if "invaliddelegate" in lowered or "invalid delegate" in lowered:
        return (
            "Session key is not the grant delegate. Re-sign DreamAgent at /agent/activate/."
        )
    if "invaliderc1271" in lowered or "invalid eoa" in lowered:
        return (
            "Delegation signature does not match DelegationManager. "
            "Re-sign DreamAgent at /agent/activate/."
        )
    if "method-not-allowed" in lowered or "allowedmethodenforcer" in lowered:
        return GRANT_MISSING_REDEEM
    if "value-too-high" in lowered:
        return "This grant cannot send native STT."
    if "expired-delegation" in lowered:
        return "This grant has expired. Re-sign DreamAgent at /agent/activate/."
    if "notdelegationmanager" in lowered:
        return "Smart Account rejected DelegationManager. Recreate the account at /agent/activate/."
    try:
        from integrations.dreamdex.client import humanize_place_order_revert

        dex = humanize_place_order_revert(exc)
        if dex and "DreamDEX rejected this order" not in dex:
            return dex
    except Exception:  # noqa: BLE001
        pass
    if decoded.strip():
        return decoded[:400]
    if raw.strip() and "08c379a0" not in raw.lower():
        return raw[:400]
    return "DreamAgent redeem was rejected on-chain."
