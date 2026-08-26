"""ERC-7710 delegation encode / validate helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from integrations.metamask import (
    EXECUTION_MODE_BATCH_DEFAULT,
    EXECUTION_MODE_SINGLE_DEFAULT,
    ROOT_AUTHORITY,
)
from integrations.metamask.smart_account import get_environment, mock_smart_account_enabled


def hash_delegation_blob(delegation: dict[str, Any]) -> str:
    """Stable hash for storage / revoke tracking (not EIP-712 digest)."""
    canonical = json.dumps(delegation, sort_keys=True, separators=(",", ":"))
    return "0x" + hashlib.sha256(canonical.encode()).hexdigest()


def validate_signed_delegation(delegation: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not isinstance(delegation, dict):
        return False, ["Delegation must be an object"]
    for key in ("delegate", "delegator", "authority", "signature"):
        if not delegation.get(key):
            reasons.append(f"Missing delegation field: {key}")
    sig = str(delegation.get("signature", ""))
    mock = mock_smart_account_enabled() or bool(delegation.get("mock"))
    if mock:
        return (len(reasons) == 0), reasons
    if sig.startswith("0xmock"):
        reasons.append("Mock signatures are not accepted on live Somnia")
    if not (sig.startswith("0x") and len(sig) >= 130):
        reasons.append("Delegation signature looks invalid")
    return (len(reasons) == 0), reasons


def build_grant_typed_data(
    *,
    chain_id: int,
    delegator: str,
    delegate: str,
    verifying_contract: str,
    max_trade_amount: str,
    max_daily_volume: str,
    expires_at: int,
    salt: str,
) -> dict[str, Any]:
    """EIP-712 payload for eth_signTypedData_v4 (DreamLens grant)."""
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "DreamAgentPermission": [
                {"name": "delegator", "type": "address"},
                {"name": "delegate", "type": "address"},
                {"name": "permission", "type": "string"},
                {"name": "maxTradeAmount", "type": "string"},
                {"name": "maxDailyVolume", "type": "string"},
                {"name": "expiresAt", "type": "uint256"},
                {"name": "salt", "type": "bytes32"},
            ],
        },
        "primaryType": "DreamAgentPermission",
        "domain": {
            "name": "DreamLens DreamAgent",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": to_checksum_address(verifying_contract),
        },
        "message": {
            "delegator": to_checksum_address(delegator),
            "delegate": to_checksum_address(delegate),
            "permission": "TRADE_EVENT_CONTRACT",
            "maxTradeAmount": str(max_trade_amount),
            "maxDailyVolume": str(max_daily_volume),
            "expiresAt": int(expires_at),
            "salt": salt if salt.startswith("0x") else "0x" + salt,
        },
    }


DELEGATION_ARRAY_ABI = (
    "(address,address,bytes32,(address,bytes,bytes)[],uint256,bytes)[]"
)
# DelegationManager.redeemDelegations takes ABI-encoded blobs, not nested tuples.
REDEEM_DELEGATIONS_SIGNATURE = "redeemDelegations(bytes[],bytes32[],bytes[])"


def _hex_to_bytes(value: str) -> bytes:
    raw = value[2:] if str(value).startswith("0x") else str(value)
    return bytes.fromhex(raw or "")


def encode_execution(target: str, call_data: str, *, value: int = 0) -> bytes:
    """ABI-encode a single Execution (target, value, callData)."""
    return encode(
        ["address", "uint256", "bytes"],
        [to_checksum_address(target), value, _hex_to_bytes(call_data)],
    )


def encode_single_execution_packed(target: str, call_data: str, *, value: int = 0) -> bytes:
    """ERC-7579 packed single Execution used by redeemDelegations."""
    addr = bytes.fromhex(to_checksum_address(target)[2:])
    return addr + int(value).to_bytes(32, "big") + _hex_to_bytes(call_data)


def encode_batch_executions(executions: list[tuple[str, int, bytes]]) -> bytes:
    """ABI-encode Execution[] for BatchDefault redeemDelegations."""
    return encode(["(address,uint256,bytes)[]"], [executions])


def encode_redeem_delegations_calldata(
    *,
    signed_delegation: dict[str, Any],
    target: str,
    call_data: str,
    value: int = 0,
    extra_executions: list[tuple[str, int, str]] | None = None,
) -> str:
    """Build redeemDelegations calldata for Delegation Manager.

    extra_executions are additional (target, value, callData) calls in the
    same redeem — used so the Smart Account can reimburse session-key gas.
    """
    caveats = signed_delegation.get("caveats") or []
    caveat_tuples = []
    for c in caveats:
        enforcer = to_checksum_address(c.get("enforcer") or ("0x" + "0" * 40))
        terms = c.get("terms") or "0x"
        args = c.get("args") or "0x"
        caveat_tuples.append((enforcer, _hex_to_bytes(terms), _hex_to_bytes(args)))

    sig = str(signed_delegation.get("signature", "0x"))
    sig_b = (
        _hex_to_bytes(sig)
        if sig.startswith("0x") and not sig.startswith("0xmock")
        else b"\x00" * 65
    )
    authority = str(signed_delegation.get("authority", ROOT_AUTHORITY))
    salt_raw = signed_delegation.get("salt", 0)
    if isinstance(salt_raw, str) and salt_raw.startswith("0x"):
        salt_int = int(salt_raw, 16)
    else:
        salt_int = int(salt_raw or 0)

    delegation_tuple = (
        to_checksum_address(signed_delegation["delegate"]),
        to_checksum_address(signed_delegation["delegator"]),
        _hex_to_bytes(authority),
        caveat_tuples,
        salt_int,
        sig_b,
    )
    permission_context = encode([DELEGATION_ARRAY_ABI], [[delegation_tuple]])

    executions: list[tuple[str, int, bytes]] = [
        (to_checksum_address(target), int(value), _hex_to_bytes(call_data))
    ]
    for extra_target, extra_value, extra_data in extra_executions or []:
        executions.append(
            (
                to_checksum_address(extra_target),
                int(extra_value),
                _hex_to_bytes(extra_data),
            )
        )
    batch = len(executions) > 1
    mode = EXECUTION_MODE_BATCH_DEFAULT if batch else EXECUTION_MODE_SINGLE_DEFAULT
    if batch:
        execution_calldata = encode_batch_executions(executions)
    else:
        t, v, d = executions[0]
        execution_calldata = encode_single_execution_packed(t, "0x" + d.hex(), value=v)

    selector = function_signature_to_4byte_selector(REDEEM_DELEGATIONS_SIGNATURE)
    encoded = encode(
        ["bytes[]", "bytes32[]", "bytes[]"],
        [
            [permission_context],
            [bytes.fromhex(mode[2:])],
            [execution_calldata],
        ],
    )
    return "0x" + (selector + encoded).hex()


def delegation_manager_address(*, chain_id: int | None = None) -> str:
    return get_environment(chain_id=chain_id).delegation_manager
