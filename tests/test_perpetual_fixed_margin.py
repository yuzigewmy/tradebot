from decimal import Decimal

from spot_agent.perpetual_market import InstrumentRules
from spot_agent.perpetual_paper import PaperAccount, d


def test_fixed_margin_never_increases_size_and_charges_notional_funding_for_both_sides():
    rules = InstrumentRules(*map(d, ("0.01", "1", "1", "1", "500", "0.004", "100")))
    account = PaperAccount({"BTC": rules, "ETH": rules}, cash=d(1000), leverage=d(30), fixed_margin=d(250), funding_charge=d("0.002"))
    quote = {"bid": 100, "ask": 100, "bid_size": 1000, "ask_size": 1000, "mark": 100}
    base = {"strategy": "channel", "closed_at": 0, "stop_loss": 98, "take_profit": 104}
    assert account.enter("BTC", base | {"side": "long"}, quote, 1) == "entered"
    assert account.enter("ETH", base | {"side": "short", "stop_loss": 102, "take_profit": 96}, quote, 1) == "entered"
    for inst in ("BTC", "ETH"):
        assert account.positions[inst].margin == Decimal("250")
        account.fund(inst, -0.0001, 100, 2)
        assert account.positions[inst].funding == Decimal("-15.000")
    assert account.equity() == Decimal("962.5000")


def test_fixed_margin_skips_if_price_size_or_cash_cannot_support_the_order():
    rules = InstrumentRules(*map(d, ("0.01", "1", "1", "1", "500", "0.004", "100")))
    candidate = {"side": "long", "strategy": "channel", "closed_at": 0, "stop_loss": 98, "take_profit": 104}
    quote = {"ask": 100, "ask_size": 74, "mark": 100}
    account = PaperAccount({"BTC": rules}, cash=d(1000), leverage=d(30), fixed_margin=d(250))
    assert account.enter("BTC", candidate, quote, 1) == "insufficient_depth"
    account.cash = d(253)
    assert account.enter("BTC", candidate, quote | {"ask_size": 75}, 1) == "insufficient_cash"
    assert not account.positions
