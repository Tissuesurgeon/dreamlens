from decimal import Decimal, ROUND_HALF_UP

from django import template

register = template.Library()


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


@register.filter
def as_usd(value):
    """Format a 0–1 outcome price as dollars, e.g. 0.43 → $0.43."""
    amount = _to_decimal(value)
    if amount is None:
        return "—"
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${quantized}"


@register.filter
def price_cents(value):
    """Alias kept for templates; displays dollars (quote is USD-pegged)."""
    return as_usd(value)


@register.filter
def spot_usd(value):
    """Format an underlying spot/opening price, e.g. 77068.2 → $77,068.20."""
    amount = _to_decimal(value)
    if amount is None:
        return "—"
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${quantized:,.2f}"


@register.filter
def usd_amount(value):
    """Format a quote/notional amount as dollars.

    Under $1,000 keep cents (testnet flow like $14.44). Larger notionals stay compact.
    """
    amount = _to_decimal(value)
    if amount is None:
        return "—"
    if amount < Decimal("1000"):
        quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"${quantized:,.2f}"
    quantized = int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"${quantized:,}"


@register.filter
def payout_mult(value):
    """Potential payout multiplier for $1 at this price, e.g. 0.43 → 2.33x."""
    amount = _to_decimal(value)
    if amount is None or amount <= 0:
        return "—"
    mult = (Decimal("1") / amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{mult}x"


@register.filter
def pct_of(value):
    """Convert 0–1 fraction to integer percent string."""
    amount = _to_decimal(value)
    if amount is None:
        return "—"
    return str(int(round(float(amount) * 100)))


@register.filter
def complement_pct(value):
    """Integer percent of the remainder (1 − value)."""
    amount = _to_decimal(value)
    if amount is None:
        return "—"
    return str(int(round((1.0 - float(amount)) * 100)))


@register.filter
def signal_label(signal_type):
    labels = {
        "STRONG_CONSENSUS": "Strong consensus",
        "MOVING_FAST": "Moving fast",
        "TRADER_DIVERGENCE": "Trader divergence",
        "EXPIRING_SOON": "Expiring soon",
        "UNUSUAL_VOLUME": "Unusual volume",
        "HIGH_LIQUIDITY": "High liquidity",
        "PRICE_IMBALANCE": "Price imbalance",
    }
    return labels.get(signal_type, signal_type.replace("_", " ").title())


@register.filter
def short_address(address):
    if not address or len(address) < 10:
        return address or ""
    return f"{address[:6]}…{address[-4:]}"
