"""Template context for DreamLens wallet / network config."""

from __future__ import annotations

import json

from django.conf import settings


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

    network_cfg = {
        "chainId": chain_id,
        "chainIdHex": hex(chain_id),
        "chainName": chain_name,
        "rpcUrl": settings.DREAMDEX_RPC_URL,
        "explorerUrl": explorer,
        "nativeCurrency": currency,
        "network": network,
    }
    return {
        "dreamlens_network": network_cfg,
        "dreamlens_network_json": json.dumps(network_cfg),
    }
