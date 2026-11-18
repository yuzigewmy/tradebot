import asyncio
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .agent import AgentError, response_text, responses_config
from .perpetual_strategy import POLICY, POLICY_ID, SKILL


class SetupReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["confirm", "wait"]
    analysis: str = Field(min_length=1, max_length=1300)
    counter_evidence: str = Field(min_length=1, max_length=650)
    evidence_ids: list[Literal["context", "setup", "execution"]] = Field(min_length=1, max_length=3)


async def review_setup(client, event, timeout):
    url, headers, settings = responses_config()
    payload = settings | {
        "instructions": SKILL,
        "input": [{"role": "user", "content": json.dumps({"policy_id": POLICY_ID, "policy": POLICY, "event": event}, ensure_ascii=False)}],
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
    return answer.model_dump() | {"model": result["model"], "service_tier": result.get("service_tier")}
