"""DreamDEX integration layer for DreamLens."""

from integrations.dreamdex.adapter import DreamDEXAdapterProtocol, get_adapter, reset_adapter
from integrations.dreamdex.exceptions import (
    DreamDEXError,
    DreamDEXNotFound,
    DreamDEXUnavailable,
    DreamDEXValidationError,
)
from integrations.dreamdex.mock import MockDreamDEXAdapter

__all__ = [
    "DreamDEXAdapterProtocol",
    "DreamDEXError",
    "DreamDEXNotFound",
    "DreamDEXUnavailable",
    "DreamDEXValidationError",
    "MockDreamDEXAdapter",
    "get_adapter",
    "reset_adapter",
]
