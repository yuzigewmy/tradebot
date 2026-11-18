"""Run genuine Skill reviews, then reprice at each response's measured arrival."""

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import time

import httpx
from pydantic import ValidationError

from .agent import AgentError
from .perpetual_history import SECOND, dump, load
from .perpetual_replay_data import DayBooks, EntryEvidence, INITIAL_DELAY, REFRESH_DELAY, second_paths
from .perpetual_review import refresh_candidate, review_event, review_setup, review_timeout
from .perpetual_strategy import PROFILES, priced_candidate


def review_signature(profile="v1"):
    config = [PROFILES[profile][1], *(os.environ.get(f"SPOT_LLM_{suffix}") for suffix in ("BASE_URL", "MODEL", "REASONING_EFFORT", "SERVICE_TIER"))]
    return hashlib.sha256(json.dumps(config).encode()).hexdigest()[:16]


async def review_entry(client, job, evidence, tick, end, *, profile="v1"):
    closed_at = job["closed_at"]
    initial_at = closed_at + INITIAL_DELAY
    trace = {"instrument": job["instrument"], "closed_at": closed_at, "initial_at": initial_at}
    initial = evidence.quote(initial_at)
    if initial is None:
        return trace | {"status": "missing_initial_quote"}
    candidate, reason = priced_candidate(job["setup"], initial, tick, closed_at, initial_at, profile=profile)
    if candidate is None:
        return trace | {"status": "price_rejected", "reason": reason, "initial_quote": initial}
    event = review_event(job, candidate, initial, None)
    trace.update(candidate=candidate, initial_quote=initial, requested_at_utc_ms=time.time_ns() // 1_000_000)
    started = time.monotonic()
    try:
        result = await review_setup(client, event, review_timeout(candidate, initial_at), profile=profile)
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        if code in (400, 401, 403, 404):
            raise AgentError(f"模型接口不可用（HTTP {code}），未替代真实研判") from None
        return trace | {"status": "model_error", "reason": f"HTTP {code}", "model_ms": (time.monotonic() - started) * SECOND}
    except (httpx.RequestError, TimeoutError, ValidationError, AgentError, json.JSONDecodeError) as error:
        trace.update(status="model_error", reason=type(error).__name__, model_ms=(time.monotonic() - started) * SECOND)
        return trace
    elapsed = (time.monotonic() - started) * SECOND
    trace.update(model_ms=elapsed, model_review=result)
    if result["decision"] == "wait":
        return trace | {"status": "model_wait"}
    ready = initial_at + math.ceil(elapsed / SECOND) * SECOND + REFRESH_DELAY
    if ready >= min(candidate["expires_at"], end):
        return trace | {"status": "expired", "verified_at": ready}
    refreshed = evidence.quote(ready)
    if refreshed is None:
        return trace | {"status": "missing_refresh_quote", "verified_at": ready}
    trace.update(verified_at=ready, verified_quote=refreshed)
    final_candidate, issue = refresh_candidate(candidate, job["setup"], refreshed, tick, evidence.forming(ready), ready, profile=profile)
    if issue:
        return trace | {"status": "revalidation_rejected", "reason": issue}
    return trace | {"status": "passed", "candidate": final_candidate, "initial_candidate": candidate}


def trace_path(root, job):
    return root / f"{job['instrument']}-{job['closed_at']}.json"


async def run_reviews(client, jobs, books, seconds, funding, ticks, root, end, workers, follow=False, *, profile="v1"):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    books = DayBooks(books)
    todo = [job for job in jobs if not trace_path(root, job).exists()]
    queue = asyncio.Queue(maxsize=workers * 2)
    completed = 0

    async def worker():
        nonlocal completed
        while (task := await queue.get()) is not None:
            job, evidence = task
            inst = job["instrument"]
            result = await review_entry(client, job, evidence, ticks[inst], end, profile=profile)
            dump(trace_path(root, job), result)
            completed += 1
            if completed % 50 == 0:
                print(f"候选研判已处理 {completed}/{len(todo)}", flush=True)

    async def produce():
        pending = todo
        while pending:
            waiting = []
            for job in pending:
                book = books.get(job)
                path, closed_at = second_paths(seconds, job)
                if book is None or not path.exists():
                    waiting.append(job)
                    continue
                if book["status"] != "ok":
                    dump(trace_path(root, job), {"instrument": job["instrument"], "closed_at": closed_at, "status": "missing_book"})
                else:
                    await queue.put((job, EntryEvidence(job, load(path), book["quotes"], funding[job["instrument"]])))
            pending = waiting
            if pending:
                if not follow:
                    raise RuntimeError(f"有 {len(pending)} 个候选未下载盘口或秒级行情")
                await asyncio.sleep(3)
        for _ in range(workers):
            await queue.put(None)

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(produce())
        for _ in range(workers):
            tasks.create_task(worker())
