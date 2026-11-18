"""Export a reproducible ledger and mark-to-market result."""

from collections import Counter, defaultdict
import csv
from dataclasses import asdict
from decimal import Decimal
import json

from .perpetual_history import utc
from .perpetual_paper import FEE_RATE, INITIAL_CASH, LEVERAGE, RISK_FRACTION, SLIPPAGE, d
from .perpetual_strategy import PROFILES


def csv_rows(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(fields)
        writer.writerows(rows)


def export(root, replay, reviews, counts, config, *, profile="v1"):
    root.mkdir(parents=True, exist_ok=True)
    account = replay.account
    peak, max_dd, worst, peak_at, dd_peak_at = INITIAL_CASH, d(0), replay.start, replay.start, replay.start
    for ts, equity, *_ in replay.equity:
        if equity > peak:
            peak, peak_at = equity, ts
        if (drawdown := (peak - equity) / peak) > max_dd:
            max_dd, worst, dd_peak_at = drawdown, ts, peak_at
    open_positions = [asdict(p) | {"mark": account.marks[inst], "unrealized_pnl": p.pnl(account.marks[inst])}
                      for inst, p in account.positions.items()]
    trades = account.trades
    funding = sum(e["amount"] for e in account.events if e["event"] == "funding")
    fees = sum(e["fee"] if e["event"] == "entry" else e["exit_fee"] for e in account.events if e["event"] in ("entry", "exit"))
    realized = sum(t["net_pnl"] for t in trades)
    wins = sum(t["net_pnl"] > 0 for t in trades)
    profit = sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0)
    loss = -sum(t["net_pnl"] for t in trades if t["net_pnl"] < 0)
    groups = defaultdict(lambda: {"count": 0, "wins": 0, "net_pnl": d(0)})
    for trade in trades:
        for group in (trade["instrument"], trade["strategy"], f"{trade['instrument']}:{trade['strategy']}", trade["side"]):
            bit = groups[group]
            bit["count"] += 1
            bit["wins"] += trade["net_pnl"] > 0
            bit["net_pnl"] += trade["net_pnl"]
    results = [row["model_review"] for row in reviews if "model_review" in row]
    tokens = Counter()
    for result in results:
        usage = result["usage"]
        if usage:
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                tokens[key] += usage[key]
    times = sorted(row["model_ms"] / 1000 for row in reviews if "model_ms" in row)
    equity = account.equity()
    policy, policy_id, _ = PROFILES[profile]
    fields = {
        "start_utc": utc(replay.start), "end_utc_exclusive": utc(replay.end), "policy_id": policy_id, "policy_version": policy["version"],
        "initial_equity": INITIAL_CASH, "ending_equity": equity, "free_cash": account.cash,
        "net_pnl": equity - INITIAL_CASH, "return_pct": (equity / INITIAL_CASH - 1) * 100,
        "realized_trade_pnl": realized, "open_positions": open_positions, "closed_trades": len(trades),
        "win_count": wins, "win_rate_pct": d(wins) / len(trades) * 100 if trades else None,
        "profit_factor": d(profit) / loss if loss else None,
        "max_drawdown_pct": max_dd * 100, "drawdown_from": utc(dd_peak_at), "drawdown_at": utc(worst),
        "total_fees": fees, "net_funding": funding,
        "leverage": LEVERAGE, "risk_fraction": RISK_FRACTION, "fee_rate": FEE_RATE, "exit_slippage": SLIPPAGE,
        "rule_candidates": len(reviews), "review_trace_counts": Counter(row["status"] for row in reviews),
        "signal_counts": counts, "entry_counts": Counter(t["state"] for t in replay.entry_log),
        "exit_counts": Counter(t["exit_reason"] for t in trades), "exit_evidence": Counter(t["exit_evidence"] for t in trades),
        "ambiguous_seconds": sum(t["ambiguous_second"] for t in trades), "minute_second_mismatches": replay.minute_mismatches,
        "groups": groups, "model_calls": len(times),
        "model_tokens_reported": tokens, "model_actual": Counter(f"{r['model']}/{r['service_tier']}" for r in results),
        "model_latency_median_s": times[len(times) // 2] if times else None,
        "model_latency_p95_s": times[min(len(times) - 1, int(len(times) * .95))] if times else None,
        "model_config": config, "rules": {i: asdict(rule) for i, rule in account.rules.items()},
        "complete_entry_data": not any(row["status"].startswith("missing_") for row in reviews),
    }
    (root / "summary.json").write_text(json.dumps(fields, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")
    columns = ("instrument", "side", "strategy", "signal_at", "opened_at", "closed_at", "trigger_at", "contracts", "base_quantity", "margin",
               "entry", "stop_loss", "take_profit", "exit_reference", "exit", "exit_reason", "exit_evidence", "ambiguous_second",
               "gross_pnl", "entry_fee", "exit_fee", "funding", "net_pnl")
    csv_rows(root / "trades.csv", ([row[k] for k in columns] for row in trades), columns)
    csv_rows(root / "equity.csv", ((utc(ts), eq, cash, n) for ts, eq, cash, n in replay.equity), ("utc", "equity", "free_cash", "open_positions"))
    (root / "events.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in account.events), encoding="utf-8")
    (root / "entries.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in replay.entry_log), encoding="utf-8")
    return fields
