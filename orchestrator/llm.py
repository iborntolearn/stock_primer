"""Claude-backed implementation of the LLM port (§6).

- Model per call ID comes from config tiers (small/medium/large).
- Structured output via `output_config.format` (JSON schema from the pydantic model),
  then re-validated with pydantic. One repair retry on schema or citation errors.
- Static preamble + task text come first so they can be prompt-cached; per-call
  data (decision time, evidence) comes last.
- Every call is budget-checked, costed and written to the llm_call audit log.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Optional

import anthropic
import pydantic

from . import prompts
from .budget import BudgetManager
from .config import TOOL_ALLOWLIST, Config
from .ports import M, Store, ToolForbidden, ToolGateway

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_EVIDENCE_REF = re.compile(r"\bE\d+\b")


class LLMCallFailed(Exception):
    def __init__(self, call_id: str, reason: str):
        super().__init__(f"{call_id}: {reason}")
        self.call_id = call_id
        self.reason = reason


def cited_ids(obj: Any) -> set[str]:
    """All evidence refs cited anywhere in an output (fields named evidence_ids)."""
    found: set[str] = set()
    if isinstance(obj, pydantic.BaseModel):
        obj = obj.model_dump()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "evidence_ids" and isinstance(v, list):
                found.update(str(x) for x in v)
            else:
                found |= cited_ids(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= cited_ids(v)
    return found


def render_request(cfg: Config, call_id: str, decision_time: datetime, sections: dict[str, str],
                   template_vars: Optional[dict[str, str]], images: Optional[list[bytes]] = None) -> tuple[str, str, str, str]:
    """(prompt version, static task text, per-call data, input hash). Shared with replay.
    Attached images (e.g. scanned filing pages) are part of the input hash."""
    version = cfg.prompt_versions.get(call_id, "v1")
    task_text = prompts.task(call_id, version, template_vars)
    data = f"<decision_time>{decision_time.isoformat()}</decision_time>\n" + "\n".join(
        f"<{name}>\n{body}\n</{name}>" for name, body in sections.items())
    h = hashlib.sha256((call_id + version + task_text + data).encode())
    for img in images or []:
        h.update(hashlib.sha256(img).digest())
    return version, task_text, data, h.hexdigest()


def image_block(img: bytes) -> dict[str, Any]:
    media = "image/png" if img[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.b64encode(img).decode()}}


def question_hash(call_id: str, version: str, decision_time: datetime, question: str) -> str:
    return hashlib.sha256((call_id + version + decision_time.isoformat() + question).encode()).hexdigest()


def _text(response: Any) -> str:
    return "".join(b.text for b in response.content if b.type == "text")


class ClaudeLLM:
    def __init__(self, config: Config, store: Store, budget: BudgetManager,
                 client: Optional[anthropic.AsyncAnthropic] = None):
        self.cfg = config
        self.store = store
        self.budget = budget
        if client is None:
            from core.settings import get_settings

            key = get_settings().anthropic_api_key       # from .env; falls back to the ANTHROPIC_API_KEY variable
            client = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()
        self.client = client

    # -- shared request plumbing ------------------------------------------------
    def _base_kwargs(self, call_id: str) -> dict[str, Any]:
        tier = self.cfg.tier_for(call_id)
        kw: dict[str, Any] = {
            "model": tier.model,
            "max_tokens": tier.max_tokens,
            "system": [{"type": "text", "text": prompts.preamble(), "cache_control": {"type": "ephemeral"}}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": tier.effort},
        }
        if tier.refusal_fallback:
            kw["betas"] = [FALLBACK_BETA]
            kw["fallbacks"] = "default"
        return kw

    async def _send(self, call_id: str, kwargs: dict[str, Any]) -> tuple[Any, int, int, float, int]:
        self.budget.check()
        t0 = time.perf_counter()
        try:
            resp = await self.client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise LLMCallFailed(call_id, f"rate_limited: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMCallFailed(call_id, f"api_error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMCallFailed(call_id, f"connection: {e}") from e
        latency = int((time.perf_counter() - t0) * 1000)
        u = resp.usage
        tin = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) + (
            getattr(u, "cache_creation_input_tokens", 0) or 0)
        tout = u.output_tokens or 0
        cost = self.cfg.cost_usd(resp.model, tin, tout)
        self.budget.charge(cost)
        return resp, tin, tout, cost, latency

    def _audit(self, *, run_id: int, agent: str, call_id: str, model: str, input_hash: str, request: dict,
               response: Any, tin: int, tout: int, cost: float, latency: int, status: str, retries: int) -> None:
        self.store.record_llm_call({
            "run_id": run_id, "agent": agent, "call_id": call_id, "model_id": model,
            "prompt_version": self.cfg.prompt_versions.get(call_id, "v1"), "input_hash": input_hash,
            "request": request, "response": response, "tokens_in": tin, "tokens_out": tout,
            "cost_usd": round(cost, 6), "latency_ms": latency, "validation_status": status, "retries": retries,
        })

    # -- structured calls -------------------------------------------------------
    async def structured(self, *, call_id: str, agent: str, run_id: int, decision_time: datetime,
                         sections: dict[str, str], output_model: type[M], cite_ids: frozenset[str] = frozenset(),
                         template_vars: Optional[dict[str, str]] = None, images: Optional[list[bytes]] = None) -> M:
        version, task_text, data, input_hash = render_request(self.cfg, call_id, decision_time, sections,
                                                               template_vars, images)
        cached = self._from_cache(call_id, input_hash, output_model, cite_ids)
        if cached is not None:
            self._audit(run_id=run_id, agent=agent, call_id=call_id, model="cache", input_hash=input_hash,
                        request={"version": version, "cache": True}, response=cached.model_dump(mode="json"),
                        tin=0, tout=0, cost=0.0, latency=0, status="cached", retries=0)
            return cached

        kwargs = self._base_kwargs(call_id)
        kwargs["output_config"] = {**kwargs["output_config"], "format": {
            "type": "json_schema", "schema": anthropic.transform_schema(output_model)}}
        messages: list[dict[str, Any]] = [{"role": "user", "content": [
            {"type": "text", "text": task_text},
            *[image_block(img) for img in images or []],
            {"type": "text", "text": data},
        ]}]

        total_in = total_out = total_latency = 0
        total_cost = 0.0
        problems: list[str] = []
        parsed: Optional[M] = None
        for attempt in range(2):
            resp, tin, tout, cost, latency = await self._send(call_id, {**kwargs, "messages": messages})
            total_in, total_out = total_in + tin, total_out + tout
            total_cost, total_latency = total_cost + cost, total_latency + latency

            if resp.stop_reason == "refusal":
                problems = [f"refusal ({getattr(resp.stop_details, 'category', None)})"]
                break
            if resp.stop_reason == "max_tokens":
                problems = ["hit max_tokens"]
                break

            raw = _text(resp)
            try:
                parsed = output_model.model_validate_json(raw)
                problems = []
            except pydantic.ValidationError as e:
                parsed = None
                problems = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:20]]

            if parsed is not None and cite_ids:
                unknown = sorted(cited_ids(parsed) - cite_ids)
                if unknown:
                    problems = [f"evidence_ids not in the supplied evidence: {', '.join(unknown[:20])}"]

            if not problems or attempt == 1:
                break
            messages = messages + [
                {"role": "assistant", "content": resp.content},
                {"role": "user", "content": "Your output failed validation:\n- " + "\n- ".join(problems)
                 + "\nReturn the corrected JSON object only. Cite only evidence IDs that appear in the inputs."},
            ]

        # A second citation failure is tolerated here: downstream validation (V2) drops unsupported claims.
        citation_only = bool(problems) and parsed is not None and problems[0].startswith("evidence_ids")
        status = "passed" if not problems and attempt == 0 else (
            "repaired" if not problems or citation_only else "failed")
        self._audit(run_id=run_id, agent=agent, call_id=call_id, model=kwargs["model"], input_hash=input_hash,
                    request={"version": version, "task": task_text, "data": data,
                             **({"images": [hashlib.sha256(i).hexdigest() for i in images]} if images else {})},
                    response=parsed.model_dump(mode="json") if parsed is not None else {"errors": problems},
                    tin=total_in, tout=total_out, cost=total_cost, latency=total_latency, status=status,
                    retries=attempt)
        if parsed is None or (problems and not citation_only):
            raise LLMCallFailed(call_id, "; ".join(problems) or "no output")
        return parsed

    def _from_cache(self, call_id: str, input_hash: str, output_model: type[M], cite_ids: frozenset[str]) -> Optional[M]:
        hours = getattr(self.cfg, "llm_cache_hours", 0) or 0
        lookup = getattr(self.store, "cached_llm_response", None)
        if hours <= 0 or lookup is None:
            return None
        resp = lookup(call_id, input_hash, hours)
        if not resp or "errors" in resp:
            return None
        try:
            parsed = output_model.model_validate(resp)
        except pydantic.ValidationError:
            return None
        if cite_ids and cited_ids(parsed) - cite_ids:
            return None
        return parsed

    # -- grounded Q&A tool loop (LLM-14) ----------------------------------------
    async def tool_answer(self, *, call_id: str, agent: str, run_id: int, decision_time: datetime,
                          question: str, tools: ToolGateway, role: str) -> tuple[str, list[str]]:
        version = self.cfg.prompt_versions.get(call_id, "v1")
        kwargs = self._base_kwargs(call_id)
        kwargs["tools"] = tools.tool_specs(TOOL_ALLOWLIST[role])
        messages: list[dict[str, Any]] = [{"role": "user", "content": [
            {"type": "text", "text": prompts.task(call_id, version)},
            {"type": "text", "text": f"<decision_time>{decision_time.isoformat()}</decision_time>\n"
                                     f"<question>\n{question}\n</question>"},
        ]}]
        seen: set[str] = set()
        tin_all = tout_all = lat_all = 0
        cost_all = 0.0
        resp = None
        for _ in range(self.cfg.qa_max_iterations):
            resp, tin, tout, cost, lat = await self._send(call_id, {**kwargs, "messages": messages})
            tin_all, tout_all, cost_all, lat_all = tin_all + tin, tout_all + tout, cost_all + cost, lat_all + lat
            if resp.stop_reason != "tool_use":
                break
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                try:
                    r = await tools.call(role, block.name, {**block.input, "as_of": decision_time.isoformat()},
                                         run_id=run_id, as_of=decision_time)
                except ToolForbidden as e:
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(e), "is_error": True})
                    continue
                if not r.ok:
                    msg = f"{r.error.code}: {r.error.message}" if r.error else "error"
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": msg, "is_error": True})
                    continue
                ref = r.evidence_ref or "unrecorded"
                seen.add(ref)
                body = json.dumps(r.data, default=str, ensure_ascii=False)
                tag = "untrusted_document" if (r.meta and r.meta.untrusted) else "evidence"
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f'<{tag} id="{ref}">{body}</{tag}>'})
            messages = messages + [{"role": "assistant", "content": resp.content},
                                   {"role": "user", "content": results}]

        status = "passed"
        if resp is None or resp.stop_reason == "refusal":
            answer, status = "I can't answer that one.", "failed"
        elif resp.stop_reason == "tool_use":
            answer, status = "I ran out of steps before finishing; try a narrower question.", "failed"
        else:
            answer = _text(resp)
        # Citations must resolve to evidence actually returned in this conversation.
        cited = sorted(set(_EVIDENCE_REF.findall(answer)))
        for ref in cited:
            if ref not in seen:
                answer = answer.replace(f"[{ref}]", "[unverified]")
                status = "repaired"
        self._audit(run_id=run_id, agent=agent, call_id=call_id, model=kwargs["model"],
                    input_hash=question_hash(call_id, version, decision_time, question),
                    request={"version": version, "question": question}, response={"answer": answer},
                    tin=tin_all, tout=tout_all, cost=cost_all, latency=lat_all, status=status, retries=0)
        return answer, [c for c in cited if c in seen]
