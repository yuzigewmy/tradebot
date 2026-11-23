import pytest

from spot_agent.perpetual_market import InstrumentRules, MarketError
from spot_agent.perpetual_paper import PaperAccount, d
from spot_agent.perpetual_regime_replay import Exit, exit_from_book


def test_short_cover_uses_ask_available_after_trigger_and_checks_full_size():
    rules = InstrumentRules(*map(d, ("0.01", "1", "1", "1", "500", "0.004", "100")))
    account = PaperAccount({"ETH": rules}, cash=d(1000), leverage=d(30), fixed_margin=d(250))
    quote = {"bid": 100, "bid_size": 100, "mark": 100}
    signal = {"side": "short", "strategy": "compression", "closed_at": 0, "stop_loss": 102, "take_profit": 96}
    assert account.enter("ETH", signal, quote, 1) == "entered"
    position = account.positions["ETH"]
    trigger = Exit("ETH", 1000, "take_profit", 96.0, "ordered_trades")
    books = {("ETH", 1000): [95.99, 96.00, 100, 100, 1000], ("ETH", 2000): [96.05, 96.06, 100, 100, 1999]}
    exit_order = exit_from_book(trigger, position, books, 1000)
    assert (exit_order.trigger_at, exit_order.at, exit_order.price) == (1000, 2000, 96.06)
    books["ETH", 2000][3] = 74
    with pytest.raises(MarketError, match="无法覆盖出场数量"):
        exit_from_book(trigger, position, books, 1000)
    books["ETH", 2000].extend(([], [(96.06, 74), (96.21, 3)]))
    exit_order = exit_from_book(trigger, position, books, 1000)
    assert exit_order.price == pytest.approx((74 * 96.06 + 96.21) / 75)
