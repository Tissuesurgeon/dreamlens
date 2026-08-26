"""Thin wrappers around the DreamDEX adapter for position reads."""

from __future__ import annotations

from integrations.dreamdex.adapter import get_adapter
from integrations.dreamdex.types import OutcomeBalancesDTO


def get_outcome_balances(account: str, market_id: str) -> OutcomeBalancesDTO:
    return get_adapter().get_outcome_balances(account, market_id)
