"""Two-stage intent routing over a local Ollama-compatible endpoint."""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass

import httpx

from ..clock import iso
from ..config import AppConfig
from .schemas import validate

log = logging.getLogger(__name__)

IS_TASK_SCHEMA = {
    "type": "object",
    "required": ["is_task"],
    "properties": {"is_task": {"type": "boolean"}},
}

ROUTE_SCHEMA = {
    "type": "object",
    "required": ["agent"],
    "properties": {
        "agent": {"type": "string", "enum": ["security-scan", "noop"]},
        "confidence": {"type": "number"},
    },
}


class LlmUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Classification:
    is_task: bool
    agent: str | None
    params: dict
    confidence: float
    source: str


class LlmRouter:
    def __init__(
        self,
        client: httpx.Client,
        cfg: AppConfig,
        conn: sqlite3.Connection | None = None,
        clock=None,
        base_url: str | None = None,
        samples: int = 3,
    ):
        self.client = client
        self.cfg = cfg
        self.conn = conn
        self.clock = clock
        self.samples = max(samples, 1)
        self.base_url = (base_url or cfg.llm.proxy).rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[: -len("/v1")]

    # ------------------------------------------------------------------ public
    def classify(self, text: str, now=None) -> Classification:
        question = {"role": "user", "content": json.dumps({"text": text[:2000]}, ensure_ascii=False)}
        is_task, conf_a = self._ask(
            self.cfg.llm.model_slm,
            [
                {"role": "system", "content": "Decide whether the text is a request to execute an action. Reply JSON only."},
                question,
            ],
            IS_TASK_SCHEMA,
            lambda obj: bool(obj.get("is_task")),
            route="task_is_task",
        )
        if not is_task:
            return Classification(False, None, {}, conf_a, "router")

        route, conf_b = self._ask(
            self.cfg.llm.model_slm,
            [
                {"role": "system", "content": "Pick the agent for this request. Reply JSON only."},
                question,
            ],
            ROUTE_SCHEMA,
            lambda obj: obj.get("agent"),
            route="task_route",
        )
        confidence = min(conf_a, conf_b)
        if confidence < self.cfg.llm.t_ask:
            return Classification(True, None, {}, confidence, "router_low_confidence")
        return Classification(True, route, {}, confidence, "router")

    # ----------------------------------------------------------------- internals
    def _chat(self, model: str, messages: list[dict], schema: dict) -> tuple[dict, float]:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0},
        }
        try:
            response = self.client.post(f"{self.base_url}/api/chat", json=payload, timeout=60)
        except httpx.HTTPError as exc:
            raise LlmUnavailable(f"{type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise LlmUnavailable(f"http_{response.status_code}")
        data = response.json()
        content = ((data.get("message") or {}).get("content") or "").strip()
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            return {}, 0.0
        confidence = 1.0
        logprobs = data.get("logprobs") or []
        if logprobs:
            try:
                confidence = min(2.718281828 ** float(item["logprob"]) for item in logprobs)
            except (KeyError, TypeError, ValueError):
                confidence = 1.0
        self._record(model, data)
        return obj, confidence

    def _ask(self, model: str, messages, schema, pick, route: str):
        votes, best_obj, best_conf = [], {}, 0.0
        for _ in range(self.samples):
            obj, conf = self._chat(model, messages, schema)
            ok, reason = validate(obj, schema)
            if not ok:
                log.info("%s schema rejected: %s", route, reason)
                continue
            value = pick(obj)
            if value is not None:
                votes.append(value)
                if conf > best_conf:
                    best_obj, best_conf = obj, conf
        if not votes:
            return None, 0.0
        winner = max(sorted(set(votes)), key=lambda value: (votes.count(value), value))
        agreement = votes.count(winner) / len(votes)
        return winner, min(agreement, best_conf) if best_conf else agreement

    def _record(self, model: str, data: dict) -> None:
        if self.conn is None or self.clock is None:
            return
        prompt_tokens = int(data.get("prompt_eval_count", 0) or 0)
        completion_tokens = int(data.get("eval_count", 0) or 0)
        cost = (
            prompt_tokens / 1000 * self.cfg.llm.price_in_per_1k
            + completion_tokens / 1000 * self.cfg.llm.price_out_per_1k
        )
        self.conn.execute(
            "INSERT INTO llm_calls(id, run_id, route, model, prompt_tokens, completion_tokens, "
            "cost_usd, latency_ms, schema_ok, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex[:16],
                None,
                "task_route",
                model,
                prompt_tokens,
                completion_tokens,
                cost,
                None,
                1,
                iso(self.clock()),
            ),
        )
        self.conn.commit()

    def daily_cost(self, now=None) -> float:
        if self.conn is None or self.clock is None:
            return 0.0
        today = iso(self.clock())[:10]
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM llm_calls WHERE created_at LIKE ?",
            (today + "%",),
        ).fetchone()
        return float(row["c"] or 0.0)
