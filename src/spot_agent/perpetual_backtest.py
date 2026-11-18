"""Run the perpetual Skill on official OKX history and replay the paper account."""

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
import httpx

from .agent import AgentError, model_configured
from .perpetual_api import INSTRUMENTS
from .perpetual_backtest_report import export
from .perpetual_book_history import acquire_books
from .perpetual_history import DAY, MINUTE, OkxHistory, dump, load, utc
from .perpetual_market import InstrumentRules, instrument_rules
from .perpetual_replay import Replay, publish_replay_signals
from .perpetual_replay_data import jobs_path, prepare_market, prepare_seconds
from .perpetual_replay_review import review_signature, run_reviews, trace_path
from .perpetual_strategy import PROFILES


def timestamp(raw):
    dt = datetime.fromisoformat(raw)
    if dt.utcoffset() is None or dt.second or dt.microsecond:
        raise argparse.ArgumentTypeError("请输入带时区、对齐到整分钟的 ISO 8601 时间")
    return int(dt.timestamp() * 1000)


async def rules_snapshot(client, path):
    if not path.exists():
        rules = await asyncio.gather(*(instrument_rules(client, inst) for inst in INSTRUMENTS))
        dump(path, {inst: {k: str(v) for k, v in asdict(rule).items()} for inst, rule in zip(INSTRUMENTS, rules, strict=True)})
    return {inst: InstrumentRules(**{k: Decimal(v) for k, v in terms.items()}) for inst, terms in load(path).items()}


async def replay_as_ready(replay, jobs, reviews_path, profile):
    per_day = defaultdict(list)
    for job in jobs:
        per_day[job["closed_at"] // DAY * DAY].append(trace_path(reviews_path, job))
    latest, counts, reviews = {}, Counter(), []
    for day in range(replay.start // DAY * DAY, replay.end, DAY):
        paths = per_day[day]
        while any(not path.exists() for path in paths):
            await asyncio.sleep(3)
        batch = [load(path) for path in paths]
        signals, batch_counts = publish_replay_signals(batch, latest, profile=profile)
        counts.update(batch_counts)
        reviews.extend(batch)
        await replay.advance(signals, min(day + DAY, replay.end))
        print(f"已回放 {utc(day)[:10]}，累计平仓 {len(replay.account.trades)} 笔", flush=True)
    return reviews, counts


async def run(options):
    start, end = options.start, options.end
    profile = options.profile
    _, policy_id, _ = PROFILES[profile]
    cache = options.cache or Path(f"data/perpetual-backtest/{start}-{end}-{policy_id}")
    market = cache / f"market-{start}-{end}"
    books = cache / ("books" if profile == "v1" else f"books-{policy_id}")
    seconds = cache / "seconds"
    reviews_path = options.review_cache or cache / f"reviews-{review_signature(profile)}"
    destination = options.out or cache / "result"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40), trust_env=True) as client:
        history = OkxHistory(client, cache)
        rules = await rules_snapshot(client, cache / f"rules-{start}-{end}.json")
        if options.stage in ("all", "data"):
            jobs = await prepare_market(history, INSTRUMENTS, start, end, market, profile)
            await asyncio.gather(prepare_seconds(history, jobs, seconds, end), acquire_books(jobs, books, options.book_workers))
        else:
            jobs = [job for inst in INSTRUMENTS for job in load(jobs_path(market, inst, profile))]
        if options.stage == "data":
            print(f"行情已准备：{len(jobs)} 个候选", flush=True)
            return
        funding = {inst: dict(load(market / f"{inst}-funding.json")) for inst in INSTRUMENTS}
        if options.stage in ("all", "review"):
            if not model_configured():
                raise AgentError("需要配置真实模型；没有设置默认确认或替代模型")
            async with httpx.AsyncClient() as model:
                await run_reviews(model, jobs, books, seconds, funding, {i: r.tick for i, r in rules.items()},
                                  reviews_path, end, options.model_workers, follow=options.follow, profile=profile)
            if options.stage == "review":
                return
        replay = Replay(history, seconds, rules, {inst: load(market / f"{inst}-trades.json") for inst in INSTRUMENTS},
                        {inst: load(market / f"{inst}-marks.json") for inst in INSTRUMENTS}, funding, start, end)
        if options.follow:
            reviews, counts = await replay_as_ready(replay, jobs, reviews_path, profile)
        else:
            reviews = [load(trace_path(reviews_path, j)) for j in jobs]
            signals, counts = publish_replay_signals(reviews, profile=profile)
            print(f"回放资金账本：{len(signals)} 条已复核信号", flush=True)
            await replay.run(signals)
        if not any("model_review" in trace for trace in reviews) and any(trace["status"] == "model_error" for trace in reviews):
            raise AgentError("没有成功的模型研判，不能据此报告策略收益")
        config = {k.lower(): os.environ.get(f"SPOT_LLM_{k}") for k in ("MODEL", "REASONING_EFFORT", "SERVICE_TIER")}
        result = export(destination, replay, reviews, counts, config, profile=profile)
        print(f"期末权益 {result['ending_equity']:.6f} USDT，收益 {result['return_pct']:+.4f}%，报告 {destination.resolve()}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=timestamp, required=True)
    parser.add_argument("--end", type=timestamp, required=True)
    parser.add_argument("--profile", choices=("v1", "v2"), default="v2")
    parser.add_argument("--stage", choices=("all", "data", "review", "report"), default="all")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--review-cache", type=Path)
    parser.add_argument("--book-workers", type=int, default=8)
    parser.add_argument("--model-workers", type=int, default=16)
    parser.add_argument("--follow", action="store_true", help="等待另一个进程准备数据或研判")
    options = parser.parse_args()
    if options.end - options.start < MINUTE or options.book_workers < 1 or options.model_workers < 1:
        parser.error("截止时间必须晚于开始时间，工作进程数须大于零")
    if options.follow and options.stage not in ("review", "report"):
        parser.error("--follow 用于 --stage review 或 --stage report")
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    asyncio.run(run(options))


if __name__ == "__main__":
    main()
