"""LLM stub service (D6): an OpenAI-compatible /v1/chat/completions endpoint that returns fixed, task-shaped
responses after a realistic delay, so load tests measure our system and not a provider's rate limits.

Responses are deterministic functions of the request (grievance.rules), not of any model. The stub can be
switched into failure modes to demonstrate the circuit breaker:  POST /admin/mode {"mode": "down"|"slow"|"ok"}.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .. import rules
from ..verification import support

app = FastAPI(title="LLM stub", version="1.0.0")
MODE = {"mode": os.getenv("STUB_MODE", "ok")}
LAT_MS = float(os.getenv("STUB_LATENCY_MS", "1200"))
JITTER = float(os.getenv("STUB_LATENCY_JITTER", "0.3"))
STUB_CALLS = Counter("stub_calls_total", "Stub calls", ["task", "mode"])
STUB_LAT = Histogram("stub_latency_seconds", "Stub latency", ["task"])
TASK_RE = re.compile(r"TASK:\s*(\w+)")
DATA_RE = re.compile(r"<<DATA>>(.*)<<END>>", re.S)


def respond(task: str, data: dict) -> dict:
    if task == "intake":
        out = rules.classify(data.get("complaint") or {})
        return {k: out[k] for k in ("product", "entity_name", "entity_type", "domain", "candidate_regulators",
                                    "split", "out_of_corpus", "summary")}
    if task == "plan":
        return rules.plan_next_action(data)
    if task == "determine":
        return rules.draft_determination(data.get("intake") or {}, data.get("evidence") or [],
                                         data.get("complaint") or {})
    if task == "verify_judge":
        det = data.get("determination") or {}
        texts = [p["text"] for p in data.get("cited_passages") or []] + list(data.get("complaint_facts") or [])
        s = support(det.get("basis", ""), texts)
        return {"supported": s >= 0.5, "unsupported_claims": [] if s >= 0.5 else [det.get("basis", "")[:120]]}
    return {"error": f"unknown task {task}"}


@app.post("/v1/chat/completions")
async def completions(req: Request):
    body = await req.json()
    msgs = body.get("messages") or []
    text = "\n".join(m.get("content", "") for m in msgs)
    task = (TASK_RE.search(text) or [None, "unknown"])[1]
    mode = MODE["mode"]
    STUB_CALLS.labels(task, mode).inc()
    t0 = time.perf_counter()
    if mode == "down":
        raise HTTPException(503, "stub provider unavailable (mode=down)")
    delay = LAT_MS / 1000 * (1 + random.uniform(-JITTER, JITTER))
    if mode == "slow":
        delay *= 30
    await asyncio.sleep(delay)
    m = DATA_RE.search(text)
    data = json.loads(m.group(1)) if m else {}
    content = json.dumps(respond(task, data), default=str)
    STUB_LAT.labels(task).observe(time.perf_counter() - t0)
    return {"id": f"stub-{int(time.time()*1000)}", "object": "chat.completion", "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": max(1, len(text) // 4), "completion_tokens": max(1, len(content) // 4)}}


@app.post("/admin/mode")
async def set_mode(payload: dict):
    if payload.get("mode") not in ("ok", "down", "slow"):
        raise HTTPException(400, "mode must be ok|down|slow")
    MODE["mode"] = payload["mode"]
    return MODE


@app.get("/health")
async def health():
    return {"status": "ok", "mode": MODE["mode"], "latency_ms": LAT_MS}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
