"""Permission / caveat builders for DreamAgent TRADE_EVENT_CONTRACT."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from integrations.metamask import PLACE_BINARY_ORDER_SELECTOR


@dataclass
class DreamAgentPermissionSpec:
    """Off-chain + on-chain permission intent (user-facing grant form)."""

    max_trade_amount: Decimal = Decimal("10")
    max_daily_volume: Decimal = Decimal("50")
    expires_at: datetime | None = None
    min_copy_score: int = 75
    allowed_traders: list[str] = field(default_factory=list)
    allowed_outcomes: list[str] = field(default_factory=list)
    allowed_contracts: list[str] = field(default_factory=list)
    permission_type: str = "TRADE_EVENT_CONTRACT"

    def agent_can(self) -> list[str]:
        return [
            "Buy Event Contract outcomes",
            "Sell/close supported positions",
            "Trade only through DreamDEX",
            "Pay Shannon gas from your Smart Account STT",
        ]

    def agent_cannot(self) -> list[str]:
        return [
            "Withdraw your funds",
            "Transfer funds to another address",
            "Change your limits",
            "Trade outside DreamDEX",
        ]

    def to_caveats_json(self) -> dict[str, Any]:
        """Caveat intent stored with the permission (enforced on-chain when live)."""
        return {
            "scope": "functionCall",
            "selectors": [PLACE_BINARY_ORDER_SELECTOR],
            "targets": list(self.allowed_contracts),
            "forbidden_selectors": [
                "transfer(address,uint256)",
                "transferFrom(address,address,uint256)",
                "approve(address,uint256)",
                "withdraw(uint256)",
            ],
            "max_trade_amount": str(self.max_trade_amount),
            "max_daily_volume": str(self.max_daily_volume),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "off_chain_only": {
                "min_copy_score": self.min_copy_score,
                "allowed_traders": list(self.allowed_traders),
                "allowed_outcomes": list(self.allowed_outcomes),
            },
        }

    def to_browser_grant_payload(self) -> dict[str, Any]:
        """Payload the browser uses with @metamask/smart-accounts-kit createDelegation."""
        return {
            "scope": {
                "type": "functionCall",
                "targets": list(self.allowed_contracts),
                "selectors": [PLACE_BINARY_ORDER_SELECTOR],
            },
            "caveats": {
                "timestamp": {
                    "beforeThreshold": (
                        int(self.expires_at.timestamp()) if self.expires_at else 0
                    ),
                },
                "max_trade_amount": str(self.max_trade_amount),
                "max_daily_volume": str(self.max_daily_volume),
            },
            "ui": {
                "agent_can": self.agent_can(),
                "agent_cannot": self.agent_cannot(),
            },
            "policy": {
                "min_copy_score": self.min_copy_score,
                "allowed_traders": list(self.allowed_traders),
                "allowed_outcomes": list(self.allowed_outcomes),
                "max_trade_amount": str(self.max_trade_amount),
                "max_daily_volume": str(self.max_daily_volume),
            },
        }
