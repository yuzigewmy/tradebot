"""Select configurations on earlier data, then replay their fixed rules."""

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
from itertools import product
from pathlib import Path

import aiohttp

from .perpetual_backtest_report import csv_rows
from .perpetual_history import MINUTE, OkxHistory, dump, load, utc
from .perpetual_regime import BRACKETS, PHASE_TWO, SLOW_BRACKETS, SLOW_TRENDS, STRATEGY_SETS, Brackets, Setup
from .perpetual_regime_replay import INSTRUMENTS, Market, Tape, replay


@dataclass(frozen=True)
class Variant:
    setup: Setup
    bracket: Brackets
    symbols: tuple
    sides: tuple = (1, -1)

    def signals(self, market):
        return market.signals((self.setup, self.bracket, self.symbols, self.sides))


@dataclass(frozen=True)
class Portfolio:
    variants: tuple
    funding_buffer: int = 0
    maximum_positions: int = 2
    cooldown: int = 0
    max_entry_drift: float | None = None

    def signals(self, market):
        return [signal for v in self.variants for signal in v.signals(market)]

    def settings(self):
        return {k: getattr(self, k) for k in ("funding_buffer", "maximum_positions", "cooldown", "max_entry_drift")}

    @classmethod
    def parse(cls, value):
        variants = tuple(Variant(Setup(**v["setup"]), Brackets(**v["bracket"]), tuple(v["symbols"]), tuple(v["sides"]))
                         for v in value["variants"])
        return cls(**(value | {"variants": variants}))


def date(value):
    return int(datetime.fromisoformat(value + "T00:00:00+08:00").timestamp() * 1000)


async def evaluate(market, portfolio, start=None, end=None, collect=False):
    return await replay(market, portfolio.signals(market), start=start, end=end, collect=collect, **portfolio.settings())


async def choose(market, out):
    candidates = {}
    splits = [market.start, date("2026-06-23"), date("2026-07-23"), market.end]
    for family, setups in STRATEGY_SETS.items():
        picks = []
        for setup, brackets, symbols, buffer in product(setups, BRACKETS[family], (INSTRUMENTS, INSTRUMENTS[:1], INSTRUMENTS[1:]), (0, 2)):
            p = Portfolio((Variant(setup, brackets, symbols),), funding_buffer=buffer)
            result = await evaluate(market, p)
            if result["trades"] >= 15:
                picks.append((result["return_pct"], p, result))
        picks.sort(key=lambda row: row[0], reverse=True)
        candidates[family] = picks[:18]
        if picks:
            score, policy, result = picks[0]
            print(f"{family:17} {len(picks):3} usable | top May–Aug {score:8.2f}% × {result['trades']:3} {policy}", flush=True)
    pairs = []
    long_modes = [v for f in ("channel", "compression", "pullback") for _,v,_ in candidates[f][:6]]
    range_modes = [v for f in ("band_reversion", "failed_breakout") for _,v,_ in candidates[f][:4]]
    for a, b, cap, buffer in product(long_modes, range_modes, (1, 2), (0, 2)):
        p = Portfolio(a.variants + b.variants, funding_buffer=buffer, maximum_positions=cap, cooldown=15, max_entry_drift=0.65)
        r = await evaluate(market,p)
        if r["trades"] >= 24:
            pairs.append((r["return_pct"],p,r))
    pairs.sort(key=lambda row: row[0],reverse=True)
    candidates["combined"] = pairs[:22]
    rows = []
    for family,picks in candidates.items():
        for _, p, _ in picks:
            r, _, _ = await evaluate(market,p,collect=True)
            sliced = [await evaluate(market,p,start=x,end=y) for x,y in zip(splits,splits[1:])]
            returns = [x["return_pct"] for x in sliced]
            score = r["return_pct"] - .7*r["max_drawdown_pct"] + .6*min(returns)
            rows.append({"family":family,"score":score,"policy":asdict(p),"development":r,"development_months":returns})
        subset = [x for x in rows if x["family"]==family]
        if subset:
            v=max(subset,key=lambda x:x["score"])
            print(f"{family:17} preferred dev {v['development']['return_pct']:8.2f}% | drawdown {v['development']['max_drawdown_pct']:.2f}% | months {[round(s,2) for s in v['development_months']]}",flush=True)
    rows.sort(key=lambda x:x["score"],reverse=True)
    dump(out/"candidates.json",rows)
    leaders=[next((v for v in rows if v["family"]==name),None) for name in candidates]
    retained=[v for v in leaders if v is not None]
    dump(out/"locked.json",[rows[0],*([r for r in retained if r!=rows[0]])])
    print(f"Locked {len(retained)} candidates using only development data",flush=True)


async def choose_phase_two(market, out, first):
    rounds = list(load(first / "candidates.json"))
    choices = []
    splits = [market.start, date("2026-06-23"), date("2026-07-23"), market.end]
    for name, setups in PHASE_TWO.items():
        screened = []
        brackets = (Brackets(1.4, 2.5, 240), Brackets(1.8, 3, 480), Brackets(2.2, 4, 720, 2.4))
        for shape, bracket, symbols, sides in product(setups, brackets, (INSTRUMENTS, INSTRUMENTS[:1], INSTRUMENTS[1:]), ((1, -1), (1,), (-1,))):
            p = Portfolio((Variant(shape, bracket, symbols, sides),), funding_buffer=2, maximum_positions=1, cooldown=30)
            r = await evaluate(market, p)
            if r["trades"] >= 12:
                screened.append((r["return_pct"], p, r))
        screened.sort(key=lambda row: row[0], reverse=True)
        for _, policy, _ in screened[:24]:
            choices.append((name, policy))
        print(f"{name:20} candidates {len(screened):4} | raw dev best {screened[0][0]:8.2f}%", flush=True)
    ranked = []
    for name, p in choices:
        r, _, _ = await evaluate(market, p, collect=True)
        months = [await evaluate(market, p, start=x, end=y) for x, y in zip(splits, splits[1:])]
        returns = [x["return_pct"] for x in months]
        score = r["return_pct"] - .9*r["max_drawdown_pct"] + .8*min(returns)
        ranked.append({"family":name,"score":score,"policy":asdict(p),"development":r,"development_months":returns})
    ranked.sort(key=lambda x:x["score"],reverse=True)
    picks = [next(row for row in ranked if row["family"] == name) for name in PHASE_TWO]
    mixtures = []
    for leader, follower in product(ranked[:15], rounds[:12]):
        a, b = Portfolio.parse(leader["policy"]), Portfolio.parse(follower["policy"])
        if a.variants == b.variants:
            continue
        p = Portfolio(a.variants + b.variants, funding_buffer=2, maximum_positions=1, cooldown=30, max_entry_drift=.75)
        r, _, _ = await evaluate(market,p,collect=True)
        months = [await evaluate(market,p,start=x,end=y) for x,y in zip(splits,splits[1:])]
        returns = [x["return_pct"] for x in months]
        score=r["return_pct"]-.9*r["max_drawdown_pct"]+.8*min(returns)
        mixtures.append({"family":"low_turnover_mix","score":score,"policy":asdict(p),"development":r,"development_months":returns})
    best_mix=max(mixtures,key=lambda row:row["score"])
    for row in (best_mix, *picks):
        print(row["family"],f"dev {row['development']['return_pct']:.2f}%", "drawdown",round(row['development']['max_drawdown_pct'],2),row["policy"],flush=True)
    dump(out/"candidates.json",[best_mix,*ranked,*mixtures])
    dump(out/"locked.json",[best_mix,*picks])


def slow_portfolios():
    for shape, bracket, symbols, funding in product(SLOW_TRENDS, SLOW_BRACKETS, (INSTRUMENTS, INSTRUMENTS[:1], INSTRUMENTS[1:]), (0, 2)):
        yield Portfolio((Variant(shape, bracket, symbols),), funding_buffer=funding, maximum_positions=1, cooldown=120)


async def choose_slow(market, out):
    scored = []
    for p in slow_portfolios():
        r = await evaluate(market, p)
        if r["trades"] >= 12:
            scored.append((r["return_pct"], p))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    shortlist = []
    for _, p in scored[:30]:
        result, _, _ = await evaluate(market, p, collect=True)
        score = result["return_pct"] - result["max_drawdown_pct"]
        shortlist.append({"family":"dual_momentum", "score":score, "development":result, "policy":asdict(p)})
    shortlist.sort(key=lambda row: row["score"],reverse=True)
    dump(out/"candidates.json",shortlist)
    dump(out/"locked.json",shortlist[:1])
    print('dual momentum',len(scored),'usable',shortlist[0]["development"]["return_pct"],shortlist[0]["policy"],flush=True)


def research_pool(earlier_outcomes):
    pool = {Portfolio.parse(row["policy"]) for folder in earlier_outcomes for row in load(folder / "candidates.json")}
    for family, shapes in (STRATEGY_SETS | PHASE_TWO).items():
        brackets = BRACKETS.get(family, (Brackets(1.4, 2.5, 240), Brackets(1.8, 3, 480), Brackets(2.2, 4, 720, 2.4)))
        for shape, bracket, symbols, sides in product(shapes, brackets, (INSTRUMENTS, INSTRUMENTS[:1], INSTRUMENTS[1:]), ((1, -1), (-1,), (1,))):
            variant = Variant(shape, bracket, symbols, sides)
            pool.add(Portfolio((variant,), funding_buffer=2, maximum_positions=1, cooldown=30))
    pool.update(slow_portfolios())
    return sorted(pool, key=repr)


async def historical_search(market, earlier, out, earlier_outcomes):
    three_windows = [market.start, date("2026-09-23"), date("2026-10-23"), market.end]
    pool = research_pool(earlier_outcomes)
    scored = []
    for p in pool:
        r = await evaluate(market,p)
        if r["trades"]>=18:
            scored.append((r["return_pct"],p,r))
    scored.sort(key=lambda item:item[0],reverse=True)
    print(f"historical search: {len(pool)} configurations, {len(scored)} at least 18 trades, {sum(r>=40 for r,_,_ in scored)} >= 40% after testing this window",flush=True)
    examined = []
    for _,p,r in scored[:160]:
        historical,_,_= await evaluate(market,p,collect=True)
        prior,_,_= await evaluate(earlier,p,collect=True)
        months = [(await evaluate(market,p,start=a,end=b))["return_pct"] for a,b in zip(three_windows,three_windows[1:])]
        score = historical["return_pct"] - historical["max_drawdown_pct"] + .75 * min(months) + .5*prior["return_pct"]
        family = p.variants[0].setup.family if len(p.variants)==1 else "combined"
        examined.append({"family":family,"score":score,"historical":historical,"development":prior,"test_months":months,"policy":asdict(p)})
    examined.sort(key=lambda row:row["score"],reverse=True)
    valid = [v for v in examined if v["historical"]["return_pct"]>=40 and v["development"]["return_pct"]>0 and min(v["test_months"])>=-10]
    selected=valid[0] if valid else examined[0]
    dump(out/"candidates.json",examined)
    dump(out/"locked.json",[selected | {"family":"historical_candidate"}])
    print('candidate after seeing the requested window',selected,flush=True)


async def choose_regimes(markets, out, earlier_outcomes):
    groups = {
        "trend": ("channel", "pullback", "impulse", "dual_momentum", "opening_range"),
        "compression": ("compression",),
        "range": ("band_reversion", "failed_breakout"),
    }
    screened = {name: [] for name in groups}
    for policy in research_pool(earlier_outcomes):
        if len(policy.variants) != 1:
            continue
        setup = policy.variants[0].setup
        group = next(name for name, families in groups.items() if setup.family in families)
        if group == "range" and setup.filtering != "range":
            continue
        if group == "trend" and setup.filtering not in ("trend", "direction", "macro"):
            continue
        results = [await evaluate(market, policy) for market in markets]
        returns = [r["return_pct"] for r in results]
        if min(r["trades"] for r in results) < 8 or min(returns) <= 0:
            continue
        screened[group].append((min(returns), policy))
    ranked = {}
    for name, entries in screened.items():
        entries.sort(key=lambda item: item[0], reverse=True)
        ranked[name] = [await rank_windows(name, policy, markets) for _, policy in entries[:24]]
        ranked[name].sort(key=lambda row: row["score"], reverse=True)
        print(name, len(entries), 'positive in both prior windows', flush=True)
    mixes = []
    subsets = [[None, *(Portfolio.parse(row["policy"]) for row in rows[:5])] for rows in ranked.values()]
    for choices in product(*subsets):
        variants = tuple(v for choice in choices if choice is not None for v in choice.variants)
        if len(variants) < 2:
            continue
        policy = Portfolio(variants, funding_buffer=2, maximum_positions=1, cooldown=30, max_entry_drift=0.75)
        mixes.append(await rank_windows("regime_combo", policy, markets))
    mixes.sort(key=lambda row: row["score"], reverse=True)
    picks = mixes[:1] + [rows[0] for rows in ranked.values() if rows]
    for row in picks:
        print(row["family"], 'prior results', [round(r["return_pct"], 2) for r in row["development_windows"]], 'score', round(row["score"], 2), flush=True)
    dump(out / "locked.json", picks)
    dump(out / "candidates.json", mixes + [row for rows in ranked.values() for row in rows])


async def rank_windows(name, policy, markets):
    results = [(await evaluate(market, policy, collect=True))[0] for market in markets]
    returns = [r["return_pct"] for r in results]
    score = min(returns) + sum(returns) / len(returns) * 0.25 - 0.7 * max(r["max_drawdown_pct"] for r in results)
    return {"family": name, "score": score, "policy": asdict(policy), "development": results[-1], "development_windows": results}


def read_books(folder):
    return {(path.name.rsplit("-", 1)[0], int(at)): quote
            for path in sorted(folder.glob("*.json")) for at, quote in load(path)["quotes"].items()}


async def validate(market, out, cache, exact, show_actual, books_dir, exit_books_dir, exit_delay_ms):
    locked = load(out / "locked.json")
    execution = {"books": read_books(books_dir) if books_dir else None,
                 "exit_books": read_books(exit_books_dir) if exit_books_dir else None,
                 "exit_delay_ms": exit_delay_ms}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=True) as client:
        tape = Tape(OkxHistory(client, cache), verify_all=exact == "all") if exact != "none" else None
        tested = []
        for pos,row in enumerate(locked):
            p=Portfolio.parse(row["policy"])
            result, trades, equity=await replay(market,p.signals(market),tape=tape, **execution, **p.settings())
            line={"family":row["family"],"test":result,"development":row["development"],"policy":row["policy"]}
            if show_actual:
                actual,_,_=await replay(market,p.signals(market),tape=tape, funding_charge=None, **execution, **p.settings())
                line["actual_funding"]=actual
            tested.append(line)
            print(f"{row['family']:17} {result['return_pct']:8.2f}% | {result['trades']:3} trades | MDD {result['max_drawdown_pct']:6.2f}% | {result['reasons']}",flush=True)
            if trades:
                fields = list(trades[0])
                csv_rows(out / f"trades-{row['family']}.csv", ([trade[key] for key in fields] for trade in trades), fields)
            if pos == 0:
                csv_rows(out / "equity.csv", ((utc(market.start + i * MINUTE), float(value)) for i, value in enumerate(equity)), ["utc", "equity"])
        dump(out/"summary.json",tested)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage",choices=("choose","choose-phase-two","choose-slow","historical-search","choose-regimes","validate"))
    parser.add_argument("--market",type=Path,default=Path("data/perpetual-regime-market"))
    parser.add_argument("--cache",type=Path,default=Path("data/okx-history"))
    parser.add_argument("--out",type=Path,default=Path("artifacts/perpetual-regime-2026-11-23"))
    parser.add_argument("--exact",choices=("none","ambiguous","all"),default="none")
    parser.add_argument("--actual-funding",action="store_true")
    parser.add_argument("--books", type=Path)
    parser.add_argument("--exit-books", type=Path)
    parser.add_argument("--exit-delay-ms", type=int, default=0)
    parser.add_argument("--first",type=Path,default=Path("artifacts/perpetual-regime-2026-11-23"))
    parser.add_argument("--second",type=Path,default=Path("artifacts/perpetual-regime-round2-2026-11-23"))
    a = parser.parse_args()
    if a.exit_books is not None and (a.exact != "all" or a.exit_delay_ms <= 0):
        parser.error("--exit-books requires --exact all and a positive --exit-delay-ms")
    a.out.mkdir(parents=True, exist_ok=True)
    rules=a.cache/"rules-1787011200000-1794960000000.json"
    split=date("2026-08-23")
    if a.stage in ("choose", "choose-phase-two", "choose-slow"):
        m=Market(a.market/"calibration",rules,date("2026-05-23"),split)
        choose_strategy = {"choose": lambda:choose(m,a.out), "choose-phase-two": lambda:choose_phase_two(m,a.out,a.first), "choose-slow": lambda:choose_slow(m,a.out)}
        asyncio.run(choose_strategy[a.stage]())
    elif a.stage == "choose-regimes":
        latest = Market(a.market / "calibration", rules, date("2026-05-23"), split)
        older = Market(a.market / "untouched", rules, date("2026-02-23"), latest.start)
        asyncio.run(choose_regimes((older, latest), a.out, (a.first, a.second)))
    else:
        m=Market(a.market,rules,split,date("2026-11-23"))
        if a.stage=="historical-search":
            earlier=Market(a.market/"calibration",rules,date("2026-05-23"),split)
            asyncio.run(historical_search(m,earlier,a.out,(a.first,a.second)))
        else:
            asyncio.run(validate(m, a.out, a.cache, a.exact, a.actual_funding, a.books, a.exit_books, a.exit_delay_ms))


if __name__=="__main__":
    main()
