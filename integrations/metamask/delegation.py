"""ERC-7710 delegation encode / validate helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from integrations.metamask import (
    EXECUTION_MODE_SINGLE_DEFAULT,
    PLACE_BINARY_ORDER_SELECTOR,
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
    typed = delegation.get("typed_data") or {}
    if isinstance(typed, dict) and typed.get("primaryType") == "DreamAgentPermission":
        reasons.append(
            "This grant was signed with a DreamLens-only typed payload. "
            "Re-sign DreamAgent at /agent/activate/ so the session key can redeem it."
        )
    caveat_fields = []
    if isinstance(typed, dict):
        for field in (typed.get("types") or {}).get("Caveat") or []:
            if isinstance(field, dict) and field.get("name"):
                caveat_fields.append(field["name"])
    if "args" in caveat_fields:
        reasons.append(
            "This grant hashed Caveat.args. DelegationManager hashes "
            "Caveat(address enforcer,bytes terms) only. Re-sign at /agent/activate/."
        )
    return (len(reasons) == 0), reasons


DELEGATION_ARRAY_ABI = (
    "(address,address,bytes32,(address,bytes,bytes)[],uint256,bytes)[]"
)
# DelegationManager.redeemDelegations takes ABI-encoded blobs, not nested tuples.
REDEEM_DELEGATIONS_SIGNATURE = "redeemDelegations(bytes[],bytes32[],bytes[])"


def _hex_to_bytes(value: str) -> bytes:
    raw = value[2:] if str(value).startswith("0x") else str(value)
    if len(raw) % 2:
        raw = "0" + raw
    return bytes.fromhex(raw or "")


def _selector(signature: str) -> bytes:
    return function_signature_to_4byte_selector(signature)


def function_call_caveats(*, expires_at: int) -> list[dict[str, str]]:
    """AllowedMethods + valueLte(0) + timestamp — matches FunctionCall scope.

    Pool addresses change every window, so targets are not pinned. The session
    key may call placeBinaryOrder, approvals, and settlement claim methods, and
    cannot send native value.
    """
    from django.conf import settings

    methods = getattr(settings, "METAMASK_ALLOWED_METHODS_ENFORCER", "") or ""
    value_lte = getattr(settings, "METAMASK_VALUE_LTE_ENFORCER", "") or ""
    timestamp = getattr(settings, "METAMASK_TIMESTAMP_ENFORCER", "") or ""
    caveats: list[dict[str, str]] = []
    if methods:
        # AllowedMethodsEnforcer.getTermsInfo reads packed 4-byte selectors, not ABI arrays.
        selectors = [
            _selector(PLACE_BINARY_ORDER_SELECTOR),
            _selector("approve(address,uint256)"),
            _selector("setOperator(address,bool)"),
            _selector("redeem(uint32,bytes32,bytes32,uint8,uint256)"),
            _selector("finalizeMarket(bytes32)"),
            _selector("pokeOracle(uint256)"),
            _selector("syncSettlement(bytes32)"),
        ]
        terms = "0x" + b"".join(selectors).hex()
        caveats.append(
            {
                "enforcer": to_checksum_address(methods),
                "terms": terms,
                "args": "0x",
            }
        )
    if value_lte:
        terms = "0x" + encode(["uint256"], [0]).hex()
        caveats.append(
            {
                "enforcer": to_checksum_address(value_lte),
                "terms": terms,
                "args": "0x",
            }
        )
    if timestamp:
        before = max(int(expires_at), 0)
        packed = (0).to_bytes(16, "big") + int(before).to_bytes(16, "big")
        caveats.append(
            {
                "enforcer": to_checksum_address(timestamp),
                "terms": "0x" + packed.hex(),
                "args": "0x",
            }
        )
    return caveats


def build_grant_typed_data(
    *,
    chain_id: int,
    delegator: str,
    delegate: str,
    verifying_contract: str,
    max_trade_amount: str,
    max_daily_volume: str,
    expires_at: int,
    salt: str | int,
) -> dict[str, Any]:
    """EIP-712 payload MetaMask signs for DelegationManager.redeemDelegations."""
    caveats = function_call_caveats(expires_at=expires_at)
    if isinstance(salt, int):
        salt_value = int(salt)
    elif str(salt).startswith("0x"):
        salt_value = int(str(salt), 16)
    else:
        salt_value = int(salt)
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            # EncoderLib: Caveat(address enforcer,bytes terms) — args are redemption-only.
            "Caveat": [
                {"name": "enforcer", "type": "address"},
                {"name": "terms", "type": "bytes"},
            ],
            "Delegation": [
                {"name": "delegate", "type": "address"},
                {"name": "delegator", "type": "address"},
                {"name": "authority", "type": "bytes32"},
                {"name": "caveats", "type": "Caveat[]"},
                {"name": "salt", "type": "uint256"},
            ],
        },
        "primaryType": "Delegation",
        "domain": {
            "name": "DelegationManager",
            "version": "1",
            "chainId": int(chain_id),
            "verifyingContract": to_checksum_address(verifying_contract),
        },
        "message": {
            "delegate": to_checksum_address(delegate),
            "delegator": to_checksum_address(delegator),
            "authority": ROOT_AUTHORITY,
            "caveats": [
                {"enforcer": c["enforcer"], "terms": c["terms"]} for c in caveats
            ],
            "salt": salt_value,
        },
        "dreamlens": {
            "permission": "TRADE_EVENT_CONTRACT",
            "maxTradeAmount": str(max_trade_amount),
            "maxDailyVolume": str(max_daily_volume),
            "expiresAt": int(expires_at),
        },
    }


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
    pre_executions: list[tuple[str, int, str]] | None = None,
) -> str:
    """Build redeemDelegations calldata for Delegation Manager.

    pre_executions run before the DreamDEX call (USDC approve).
    extra_executions run after (session-key gas reimbursement).
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

    executions: list[tuple[str, int, bytes]] = []
    for pre_target, pre_value, pre_data in pre_executions or []:
        executions.append(
            (
                to_checksum_address(pre_target),
                int(pre_value),
                _hex_to_bytes(pre_data),
            )
        )
    executions.append(
        (to_checksum_address(target), int(value), _hex_to_bytes(call_data))
    )
    for extra_target, extra_value, extra_data in extra_executions or []:
        executions.append(
            (
                to_checksum_address(extra_target),
                int(extra_value),
                _hex_to_bytes(extra_data),
            )
        )
    # AllowedMethods / ValueLte only accept Single + Default. One redeem tx can
    # still carry several permission contexts, each with SingleDefault calldata
    # (approve, then placeBinaryOrder). BatchDefault Execution[] reverts those
    # enforcers. See MetaMask DelegationManager + AllowedMethodsEnforcer.
    packed: list[bytes] = []
    for target_addr, exec_value, exec_data in executions:
        packed.append(
            encode_single_execution_packed(
                target_addr, "0x" + exec_data.hex(), value=exec_value
            )
        )
    mode = bytes.fromhex(EXECUTION_MODE_SINGLE_DEFAULT[2:])
    selector = function_signature_to_4byte_selector(REDEEM_DELEGATIONS_SIGNATURE)
    encoded = encode(
        ["bytes[]", "bytes32[]", "bytes[]"],
        [
            [permission_context] * len(packed),
            [mode] * len(packed),
            packed,
        ],
    )
    return "0x" + (selector + encoded).hex()


def delegation_manager_address(*, chain_id: int | None = None) -> str:
    return get_environment(chain_id=chain_id).delegation_manager
