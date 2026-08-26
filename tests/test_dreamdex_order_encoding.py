"""DreamDEX placeBinaryOrder encoding matches the official OrderBook enum."""

from __future__ import annotations

from decimal import Decimal

from eth_abi import decode
from eth_utils import keccak

from integrations.dreamdex.client import (
    _ORDER_TYPE,
    _PLACE_BINARY_TYPES,
    _REDEEM_TYPES,
    align_quantity_raw,
    encode_module_redeem,
    encode_place_binary_order,
    humanize_place_order_revert,
)
from integrations.dreamdex.exceptions import DreamDEXValidationError
from integrations.dreamdex.types import TradeIntent


def _decode_place_binary(data: str):
    raw = bytes.fromhex(data[2:])
    return decode(_PLACE_BINARY_TYPES, raw[4:])


def test_sdk_order_type_enum():
    assert _ORDER_TYPE["LIMIT"] == 0
    assert _ORDER_TYPE["FILL_OR_KILL"] == 1
    assert _ORDER_TYPE["MARKET"] == 2  # ImmediateOrCancel, not FillOrKill
    assert _ORDER_TYPE["POST_ONLY"] == 3


def test_immediate_or_cancel_no_fill_selector():
    assert keccak(text="ImmediateOrCancelNoFill()")[:4].hex() == "d48c4403"
    assert "No liquidity" in humanize_place_order_revert(Exception("0xd48c4403"))
    assert keccak(text="FillOrKillNotFillable()")[:4].hex() == "c04ad919"
    assert "cannot fill the full size" in humanize_place_order_revert(
        Exception("0xc04ad919")
    )


def test_invalid_quantity_selector_and_lot_align():
    assert keccak(text="InvalidQuantity(uint256,uint256)")[:4].hex() == "4f174b29"
    assert "lot grid" in humanize_place_order_revert(Exception("0x4f174b29"))
    assert align_quantity_raw(17_857_143, lot=1000, min_qty=1000) == 17_857_000
    try:
        align_quantity_raw(500, lot=1000, min_qty=1000)
        assert False, "expected DreamDEXValidationError"
    except DreamDEXValidationError as exc:
        assert "minimum size" in str(exc)


def test_market_buy_yes_encodes_ioc_with_crossing_price(settings):
    settings.MOCK_DREAMDEX = False
    from integrations.dreamdex.client import LiveDreamDEXClient

    client = LiveDreamDEXClient()
    unsigned = client.prepare_place_order(
        TradeIntent(
            market_id="0x" + "11" * 32,
            pool="0xF501C195eCF6b15676cd6b5419986aC94B2022f3",
            side="BUY_YES",
            price=Decimal("0.50"),
            quantity=Decimal("20"),
            order_type="MARKET",
        )
    )
    kind, price_raw, qty_raw, _expire, order_type, *_rest = _decode_place_binary(
        unsigned.data
    )
    assert kind == 0
    assert order_type == 2
    assert price_raw == 10 ** int(settings.DREAMDEX_COLLATERAL_DECIMALS) - int(
        settings.DREAMDEX_TICK
    )
    assert qty_raw == 20 * (10 ** int(settings.DREAMDEX_COLLATERAL_DECIMALS))


def test_market_buy_no_uses_one_tick_yes_price(settings):
    from integrations.dreamdex.client import LiveDreamDEXClient

    client = LiveDreamDEXClient()
    unsigned = client.prepare_place_order(
        TradeIntent(
            market_id="0x" + "22" * 32,
            pool="0xF501C195eCF6b15676cd6b5419986aC94B2022f3",
            side="BUY_NO",
            price=Decimal("0.50"),
            quantity=Decimal("10"),
            order_type="MARKET",
        )
    )
    kind, price_raw, _qty, _expire, order_type, *_rest = _decode_place_binary(
        unsigned.data
    )
    assert kind == 2
    assert order_type == 2
    assert price_raw == int(settings.DREAMDEX_TICK)


def test_encode_helper_round_trip():
    data = encode_place_binary_order(
        kind=0,
        price_raw=500000,
        qty_raw=20_000_000,
        expire_ns=1_700_000_000 * 1_000_000_000,
        order_type=2,
    )
    decoded = _decode_place_binary(data)
    assert decoded[0] == 0
    assert decoded[1] == 500000
    assert decoded[4] == 2


def test_module_redeem_matches_sdk_abi():
    venue = "0x679795a0195a1b76cdebb7c51d74e058aee92919b8c3389af86ef24535e8a28c"
    market_id = "0x" + "11" * 32
    data = encode_module_redeem(
        market_id=market_id,
        amount=20_000_000,
        outcome_idx=0,
        venue_id=venue,
        operator_id=0,
    )
    raw = bytes.fromhex(data[2:])
    assert keccak(text="redeem(uint32,bytes32,bytes32,uint8,uint256)")[:4] == raw[:4]
    operator_id, venue_b, market_b, outcome_idx, amount = decode(_REDEEM_TYPES, raw[4:])
    assert operator_id == 0
    assert venue_b == bytes.fromhex(venue[2:])
    assert market_b == bytes.fromhex(market_id[2:])
    assert outcome_idx == 0
    assert amount == 20_000_000
