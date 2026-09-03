"""Template context for DreamLens wallet / network config."""

from __future__ import annotations

import json

from django.conf import settings

from integrations.telegram.client import bot_url
from services.event_copy import collateral_ticker


def dreamlens(request):
    chain_id = int(settings.DREAMDEX_CHAIN_ID)
    network = (settings.DREAMDEX_NETWORK or "testnet").lower()
    if network == "mainnet":
        chain_name = "Somnia Mainnet"
        currency = {"name": "Somnia", "symbol": "SOMI", "decimals": 18}
        explorer = "https://explorer.somnia.network"
    else:
        chain_name = "Somnia Shannon Testnet"
        currency = {"name": "Somnia Test Token", "symbol": "STT", "decimals": 18}
        explorer = "https://shannon-explorer.somnia.network"

    ticker = collateral_ticker()
    from services.onboarding_service import first_session_state

    try:
        state = first_session_state(getattr(request, "user", None))
    except Exception:
        state = {
            "incomplete": False,
            "can_trade": False,
            "step": "connect",
            "step_index": 1,
            "next_url": "/agent/activate/",
        }
    network_cfg = {
        "chainId": chain_id,
        "chainIdHex": hex(chain_id),
        "chainName": chain_name,
        "rpcUrl": settings.DREAMDEX_RPC_URL,
        "explorerUrl": explorer,
        "nativeCurrency": currency,
        "network": network,
        "collateralSymbol": ticker,
        "agentCanTrade": bool(state.get("can_trade")),
    }
    return {
        "dreamlens_network": network_cfg,
        "dreamlens_network_json": json.dumps(network_cfg),
        "collateral_symbol": ticker,
        "telegram_bot_url": bot_url(),
        "first_session": state,
    }
