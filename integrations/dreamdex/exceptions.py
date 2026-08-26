"""DreamDEX integration exceptions."""


class DreamDEXError(Exception):
    """Base error for DreamDEX integration failures."""


class DreamDEXNotFound(DreamDEXError):
    """Requested market, fill, or resource was not found."""


class DreamDEXUnavailable(DreamDEXError):
    """DreamDEX indexer or RPC is unavailable, or the live adapter is not yet implemented."""


class DreamDEXValidationError(DreamDEXError):
    """Trade intent or request parameters failed validation."""
