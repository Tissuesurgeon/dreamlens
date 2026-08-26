"""Smart account address derivation and environment config."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from django.conf import settings
from eth_utils import to_checksum_address


class SmartAccountConfigError(Exception):
    """Delegation Framework is not configured for live use."""


@dataclass(frozen=True)
class SmartAccountsEnvironment:
    """Delegation Framework addresses for a chain (custom Somnia deploy)."""

    chain_id: int
    delegation_manager: str
    simple_factory: str
    hybrid_implementation: str = ""
    entry_point: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.delegation_manager and self.simple_factory)


def get_environment(*, chain_id: int | None = None) -> SmartAccountsEnvironment:
    cid = chain_id or int(getattr(settings, "DREAMDEX_CHAIN_ID", 50312))
    return SmartAccountsEnvironment(
        chain_id=cid,
        delegation_manager=getattr(settings, "METAMASK_DELEGATION_MANAGER", "") or "",
        simple_factory=getattr(settings, "METAMASK_SIMPLE_FACTORY", "") or "",
        hybrid_implementation=getattr(settings, "METAMASK_HYBRID_IMPL", "") or "",
        entry_point=getattr(settings, "METAMASK_ENTRY_POINT", "") or "",
    )


def mock_smart_account_enabled() -> bool:
    """True only when tests/settings explicitly force mock — never because env is empty."""
    return bool(getattr(settings, "MOCK_SMART_ACCOUNT", False))


def require_live_environment(*, chain_id: int | None = None) -> SmartAccountsEnvironment:
    env = get_environment(chain_id=chain_id)
    if mock_smart_account_enabled():
        return env
    if not env.configured:
        raise SmartAccountConfigError(
            "MetaMask Delegation Framework is not configured. "
            "Deploy on Shannon (scripts/metamask/spike_deploy_and_trade.mjs) "
            "and set METAMASK_DELEGATION_MANAGER + METAMASK_SIMPLE_FACTORY."
        )
    return env


def derive_counterfactual_address(owner_address: str, *, salt: str = "0x") -> str:
    """Test-only deterministic SA address. Live mode requires a factory address from the client."""
    digest = hashlib.sha256(
        f"dreamlens-sa:{owner_address.lower()}:{salt.lower()}".encode()
    ).hexdigest()
    return to_checksum_address("0x" + digest[-40:])


def normalize_address(address: str) -> str:
    """Checksum when valid hex; otherwise return a stable 0x-prefixed form (tests)."""
    raw = (address or "").strip()
    if not raw:
        raise ValueError("address required")
    if not raw.startswith("0x") and not raw.startswith("0X"):
        raw = "0x" + raw
    try:
        return to_checksum_address(raw)
    except (ValueError, TypeError):
        if not mock_smart_account_enabled():
            raise ValueError(f"Invalid address: {address}") from None
        body = raw[2:]
        if len(body) < 40:
            body = body.ljust(40, "0")
        return "0x" + body[:40]


def eip1167_init_code(implementation: str) -> str:
    impl = normalize_address(implementation)[2:].lower()
    return (
        "0x3d602d80600a3d3981f3363d3d373d3d3d363d73"
        + impl
        + "5af43d82803e903d91602b57fd5bf3"
    )


def predict_create2_address(factory: str, salt_bytes: bytes, init_code: str) -> str:
    from eth_utils import keccak

    init_hash = keccak(bytes.fromhex(init_code[2:]))
    packed = b"\xff" + bytes.fromhex(normalize_address(factory)[2:]) + salt_bytes + init_hash
    return to_checksum_address(keccak(packed)[-20:])


def owner_deploy_salt(owner_address: str) -> str:
    digest = hashlib.sha256(f"dreamlens-hybrid:{owner_address.lower()}".encode()).hexdigest()
    return "0x" + digest


def encode_hybrid_initialize(owner_address: str) -> bytes:
    from eth_abi import encode
    from eth_utils import function_signature_to_4byte_selector

    owner = normalize_address(owner_address)
    selector = function_signature_to_4byte_selector(
        "initialize(address,string[],uint256[],uint256[])"
    )
    encoded = encode(
        ["address", "string[]", "uint256[]", "uint256[]"],
        [owner, [], [], []],
    )
    return selector + encoded


def encode_erc1967_proxy_creation_code(*, implementation: str, initcode: bytes) -> str:
    from eth_abi import encode

    from integrations.metamask.erc1967_proxy import ERC1967_PROXY_BYTECODE

    impl = normalize_address(implementation)
    ctor = encode(["address", "bytes"], [impl, initcode])
    return ERC1967_PROXY_BYTECODE + ctor.hex()


def encode_factory_deploy_tx(
    *,
    implementation: str,
    salt: str,
    owner_address: str,
) -> dict[str, str]:
    """Unsigned SimpleFactory.deploy(ERC1967Proxy + Hybrid.initialize) for MetaMask."""
    from eth_abi import encode
    from eth_utils import function_signature_to_4byte_selector

    env = get_environment()
    if not env.simple_factory:
        raise SmartAccountConfigError("METAMASK_SIMPLE_FACTORY is not set")
    if not owner_address:
        raise ValueError("owner_address is required to deploy a Hybrid Smart Account")
    initcode = encode_hybrid_initialize(owner_address)
    creation = encode_erc1967_proxy_creation_code(
        implementation=implementation,
        initcode=initcode,
    )
    salt_hex = salt if str(salt).startswith("0x") else "0x" + str(salt)
    salt_bytes = bytes.fromhex(salt_hex[2:].zfill(64)[:64])
    selector = function_signature_to_4byte_selector("deploy(bytes,bytes32)")
    encoded = encode(
        ["bytes", "bytes32"],
        [bytes.fromhex(creation[2:]), salt_bytes],
    )
    predicted = predict_create2_address(env.simple_factory, salt_bytes, creation)
    return {
        "to": env.simple_factory,
        "data": "0x" + (selector + encoded).hex(),
        "value": "0x0",
        "chain_id": hex(env.chain_id),
        "salt": "0x" + salt_bytes.hex(),
        "predicted_address": predicted,
        "already_deployed": False,
    }


def probe_hybrid_account(address: str) -> dict:
    """On-chain Hybrid proxy probe. Mock / RPC errors return empty (not deployed)."""
    empty = {"code_size": 0, "owner": "", "implementation": ""}
    if mock_smart_account_enabled() or not (address or "").strip():
        return empty
    try:
        from eth_utils import function_signature_to_4byte_selector
        from web3 import Web3

        rpc = getattr(settings, "DREAMDEX_RPC_URL", "") or ""
        if not rpc:
            return empty
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
        addr = normalize_address(address)
        code = w3.eth.get_code(addr)
        if not code:
            return empty
        slot = int("360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc", 16)
        impl_raw = w3.eth.get_storage_at(addr, slot)
        impl_hex = "0x" + impl_raw[-20:].hex()
        implementation = ""
        if impl_hex != "0x" + ("00" * 20):
            implementation = to_checksum_address(impl_hex)
        owner = ""
        try:
            owner_data = w3.eth.call(
                {
                    "to": addr,
                    "data": "0x" + function_signature_to_4byte_selector("owner()").hex(),
                }
            )
            if owner_data and len(owner_data) >= 20:
                owner = to_checksum_address("0x" + owner_data[-20:].hex())
        except Exception:  # noqa: BLE001
            owner = ""
        return {
            "code_size": len(code),
            "owner": owner,
            "implementation": implementation,
        }
    except Exception:  # noqa: BLE001
        return empty


def collateral_token_address() -> str:
    raw = getattr(settings, "DREAMDEX_COLLATERAL", "") or (
        "0x70a86D8842FB63C4Ad2b7cdddF530eBf1BB25d8E"
    )
    return to_checksum_address(raw)
