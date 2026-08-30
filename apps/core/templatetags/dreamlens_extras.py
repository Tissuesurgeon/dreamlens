from decimal import Decimal, ROUND_HALF_UP

from django import template

from services.event_copy import (
    SCORE_DISCLAIMER,
    as_cents as _as_cents,
    collateral_ticker,
    event_is_open as _event_is_open,
    event_question as _event_question,
    event_window_copy as _event_window_copy,
    format_collateral,
    format_ends_in as _format_ends_in,
    format_usd_plain,
    format_window_line as _format_window_line,
    put_one_win,
)

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
    return format_collateral(value)


@register.filter
def price_cents(value):
    """Outcome price as dollars, e.g. 0.41 → $0.41."""
    return _as_cents(value)


@register.filter
def as_cents(value):
    return _as_cents(value)


@register.filter
def question_copy(event):
    return _event_question(event)


@register.filter
def put_one(value):
    return put_one_win(value)


@register.filter
def ends_in(event):
    return _format_ends_in(event)


@register.filter
def window_line(event):
    return _format_window_line(event)


@register.filter
def window_kicker(event):
    return _event_window_copy(event)["kicker"]


@register.filter
def window_blurb(event):
    return _event_window_copy(event)["blurb"]


@register.filter
def window_closed_label(event):
    return _event_window_copy(event)["closed_label"]


@register.filter
def window_is_open(event):
    return _event_is_open(event)


@register.filter
def usd_plain(value):
    """Stake / payout / PnL in Event Contract collateral."""
    return format_collateral(value)


@register.simple_tag
def score_disclaimer():
    return SCORE_DISCLAIMER


@register.simple_tag
def collateral_symbol():
    return collateral_ticker()


@register.filter
def spot_usd(value):
    """Format an underlying spot/opening price, e.g. 77068.2 → $77,068.20."""
    return format_usd_plain(value)


@register.filter
def usd_amount(value):
    """Quote/notional in Event Contract collateral."""
    return format_collateral(value)


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
