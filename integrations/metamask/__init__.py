"""MetaMask Smart Accounts / ERC-7710 delegation helpers (Python side)."""

from __future__ import annotations

ROOT_AUTHORITY = (
    "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
)

# placeBinaryOrder selector used by DreamDEX pools
PLACE_BINARY_ORDER_SELECTOR = (
    "placeBinaryOrder(uint8,uint256,uint256,uint64,uint8,uint8,address,uint96,uint64)"
)

# ExecutionMode from Delegation Framework
EXECUTION_MODE_SINGLE_DEFAULT = (
    "0x0000000000000000000000000000000000000000000000000000000000000000"
)
EXECUTION_MODE_BATCH_DEFAULT = (
    "0x0100000000000000000000000000000000000000000000000000000000000000"
)
