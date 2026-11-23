"""Price and funding bounds shared by research and live signal calculation."""

from decimal import ROUND_CEILING, ROUND_FLOOR

from .perpetual_history import MINUTE
from .perpetual_market import FUNDING_INTERVAL
from .perpetual_paper import d

CAPITAL, MARGIN, LEVERAGE = (d(n) for n in (1000, 250, 30))
FUNDING_CHARGE = d("0.002")
MIN_STOP_PCT, MAX_STOP_PCT = 0.004, 0.024


def adverse_tick(price, sign, tick, slip):
    price = d(price) * (1 + sign * slip)
    return (price / tick).to_integral_value(rounding=ROUND_CEILING if sign == 1 else ROUND_FLOOR) * tick


def price_brackets(entry, sign, atr, brackets):
    distance = min(MAX_STOP_PCT * float(entry), max(MIN_STOP_PCT * float(entry), brackets.stop_atr * atr))
    return entry - sign * d(distance), entry + sign * d(distance) * d(brackets.target_r)


def holding_deadline(entry_at, hold_minutes, funding_buffer):
    deadline = entry_at + hold_minutes * MINUTE
    if funding_buffer:
        settlement = (entry_at // FUNDING_INTERVAL + 1) * FUNDING_INTERVAL
        deadline = min(deadline, settlement - funding_buffer * MINUTE)
    return deadline
