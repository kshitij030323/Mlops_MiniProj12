"""Provider-agnostic LLM client behind an interface we control (A5, L5).

Speaks the OpenAI-compatible /chat/completions protocol, so the provider (Groq, Gemini's OpenAI endpoint,
OpenRouter, Ollama, or the local stub) is chosen purely by LLM_BASE_URL / LLM_MODEL / LLM_API_KEY.
Every call has a timeout, bounded retry with exponential backoff + jitter, and goes through a circuit breaker.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass

import httpx

from .config import settings
from .observability import (BREAKER_STATE, LLM_CALLS, LLM_COST, LLM_LATENCY, LLM_RETRIES, LLM_TOKENS, log,
                            tracer)


class LLMUnavailable(Exception):
    """Provider failed, timed out, or the breaker is open. Callers must take the degradation path."""


class CircuitBreaker:
    """Classic closed -> open -> half-open breaker. State is per worker process."""

    def __init__(self, name: str, fail_threshold: int, reset_after_s: float):
        self.name, self.fail_threshold, self.reset_after_s = name, fail_threshold, reset_after_s
        self.failures = 0
        self.opened_at: float | None = None
        BREAKER_STATE.labels(name).set(0)

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if time.monotonic() - self.opened_at >= self.reset_after_s:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        if self.opened_at is not None:
            log.info("circuit_breaker_closed", breaker=self.name)
        self.failures, self.opened_at = 0, None
        BREAKER_STATE.labels(self.name).set(0)

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.fail_threshold:
            self.opened_at = time.monotonic()
            BREAKER_STATE.labels(self.name).set(1)
            log.warning("circuit_breaker_opened", breaker=self.name, failures=self.failures)

    def snapshot(self) -> dict:
        return {"name": self.name, "state": self.state, "failures": self.failures,
                "fail_threshold": self.fail_threshold, "reset_after_s": self.reset_after_s}


_breaker: CircuitBreaker | None = None


def breaker() -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        s = settings()
        _breaker = CircuitBreaker("llm_provider", s.breaker_fail_threshold, s.breaker_reset_s)
    return _breaker


@dataclass
class LLMResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_s: float
    retries: int


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


async def chat(messages: list[dict], *, agent: str, step: str, correlation_id: str = "-",
               temperature: float = 0.0, max_tokens: int = 700) -> LLMResult:
    s = settings()
    br = breaker()
    if not br.allow():
        LLM_CALLS.labels(agent, step, "breaker_open").inc()
        raise LLMUnavailable(f"circuit breaker open ({br.snapshot()})")

    headers = {"Content-Type": "application/json", "X-Correlation-ID": correlation_id}
    if s.llm_api_key:
        headers["Authorization"] = f"Bearer {s.llm_api_key}"
    body = {"model": s.llm_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    url = f"{s.llm_base_url.rstrip('/')}/chat/completions"

    last_err: Exception | None = None
    t0 = time.perf_counter()
    with tracer().start_as_current_span(f"llm.{step}") as span:
        span.set_attribute("llm.agent", agent)
        for attempt in range(s.llm_max_retries + 1):
            if attempt:
                LLM_RETRIES.labels(agent, step).inc()
                delay = s.llm_backoff_base_s * (2 ** (attempt - 1)) * (1 + random.random() * 0.25)
                await asyncio.sleep(delay)
                if not br.allow():
                    break
            try:
                r = await _http().post(url, json=body, headers=headers, timeout=s.llm_timeout_s)
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"provider status {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                data = r.json()
                content = data["choices"][0]["message"]["content"] or ""
                usage = data.get("usage") or {}
                pt = int(usage.get("prompt_tokens") or _estimate_tokens(json.dumps(messages)))
                ct = int(usage.get("completion_tokens") or _estimate_tokens(content))
                cost = pt / 1000 * s.price_prompt_per_1k + ct / 1000 * s.price_completion_per_1k
                lat = time.perf_counter() - t0
                br.record_success()
                LLM_CALLS.labels(agent, step, "ok").inc()
                LLM_LATENCY.labels(step).observe(lat)
                LLM_TOKENS.labels(agent, step, "prompt").inc(pt)
                LLM_TOKENS.labels(agent, step, "completion").inc(ct)
                LLM_COST.labels(agent, step).inc(cost)
                span.set_attribute("llm.prompt_tokens", pt)
                span.set_attribute("llm.completion_tokens", ct)
                span.set_attribute("llm.retries", attempt)
                return LLMResult(content, pt, ct, cost, lat, attempt)
            except (httpx.HTTPError, KeyError, ValueError) as e:
                last_err = e
                br.record_failure()
                log.warning("llm_call_failed", agent=agent, step=step, attempt=attempt, error=str(e)[:200],
                            breaker=br.state)
        LLM_CALLS.labels(agent, step, "failed").inc()
        span.set_attribute("llm.failed", True)
    raise LLMUnavailable(f"LLM call failed after retries: {last_err}")


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_json(text: str) -> dict:
    """Extract the first JSON object from a model reply (models often wrap JSON in prose or fences)."""
    m = _JSON_RE.search(text or "")
    if not m:
        raise ValueError("no JSON object in model output")
    return json.loads(m.group(0))
