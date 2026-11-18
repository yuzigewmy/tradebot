import asyncio
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .agent import AgentError, response_text, responses_config
from .perpetual_strategy import PROFILES, candidate_issue, exits_touched, priced_candidate


class SetupReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["confirm", "wait"]
    analysis: str = Field(min_length=1, max_length=1300)
    counter_evidence: str = Field(min_length=1, max_length=650)
    evidence_ids: list[Literal["context", "setup", "execution"]] = Field(min_length=1, max_length=3)


def review_event(job, candidate, quote, reason):
    event = {key: job[key] for key in ("instrument", "closed_at", "context", "setup")}
    event["execution"] = {"candidate": candidate, "quote": quote, "observation_reason": reason}
    return event


def review_timeout(candidate, now):
    return min(40, (candidate["expires_at"] - now) / 1000) if candidate else 40


def revalidation_issue(candidate, setup, quote, forming, now, *, profile="v1"):
    issue = candidate_issue(candidate, setup, quote, now, profile=profile)
    if not issue and exits_touched(candidate, forming):
        return "复核期间成交已触及退出价位"
    return issue


def refresh_candidate(candidate, setup, quote, tick, forming, now, *, profile="v1"):
    if profile == "v1":
        issue = revalidation_issue(candidate, setup, quote, forming, now)
        return (None, issue) if issue else (candidate, None)
    if exits_touched(candidate, forming):
        return None, "复核期间成交已触及退出价位"
    refreshed, issue = priced_candidate(setup, quote, tick, candidate["closed_at"], now, profile=profile)
    if issue:
        return None, issue
    # Recheck both the refreshed levels and the price path since the trigger.
    issue = revalidation_issue(refreshed, setup, quote, forming, now, profile=profile)
    return (None, issue) if issue else (refreshed, None)


async def review_setup(client, event, timeout, *, profile="v1"):
    policy, policy_id, skill = PROFILES[profile]
    url, headers, settings = responses_config()
    payload = settings | {
        "instructions": skill,
        "input": [{"role": "user", "content": json.dumps({"policy_id": policy_id, "policy": policy, "event": event}, ensure_ascii=False)}],
        "max_output_tokens": 3800,
        "text": {"format": {"type": "json_schema", "name": "perpetual_review", "strict": True, "schema": SetupReview.model_json_schema()}},
    }
    async with asyncio.timeout(timeout):
        response = await client.post(url, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if result["status"] != "completed":
        raise AgentError(f"模型研判未完成（{result['status']}）")
    answer = SetupReview.model_validate_json(response_text(result["output"]))
    if answer.decision == "confirm" and (event["execution"]["candidate"] is None or not {"setup", "execution"} <= set(answer.evidence_ids)):
        raise AgentError("模型确认未引用完整的开仓及报价证据")
    if re.search(r"\d|[%％]", answer.analysis + answer.counter_evidence):
        raise AgentError("模型解释含未核验的数值")
    return answer.model_dump() | {"model": result["model"], "service_tier": result.get("service_tier"), "usage": result.get("usage")}
