"""Agent worker (scalable: docker compose up --scale agent-worker=3).

Pulls run jobs from the Redis queue, executes the LangGraph agents, persists the full step trace and result.
Concurrency inside one worker is asyncio-based (runs are dominated by waiting on the model).
"""
from __future__ import annotations

import asyncio
import os
import random
import socket
import time

import httpx
from prometheus_client import start_http_server

from .. import store
from ..agents.graph import ESCALATION_PATH, RunContext, run_graph
from ..config import settings
from ..observability import (RUN_DURATION, RUNS_FINISHED, RUNS_STARTED, STEPS_PER_RUN, TOOL_CALLS_PER_RUN, LIVE_QUALITY,
                             LIVE_QUALITY_LAST, correlation_id_var, extract_trace_context, inject_trace_context, log, run_id_var,
                             setup_logging, setup_tracing, tracer)
from ..retrieval import retriever

HOST = socket.gethostname()


async def call_verifier(payload: dict, correlation_id: str) -> dict:
    s = settings()
    async with httpx.AsyncClient(timeout=s.verifier_timeout_s) as c:
        r = await c.post(f"{s.verifier_url.rstrip('/')}/verify", json=payload,
                         headers={"X-Correlation-ID": correlation_id, **inject_trace_context()})
        r.raise_for_status()
        return r.json()


def live_scores(final: dict, state: dict) -> dict:
    """Reference-free versions of the evaluation metrics, for sampled live runs (L6)."""
    v = state.get("verification") or {}
    det = final.get("determination") or {}
    ev = state.get("evidence") or []
    reg = det.get("regulator")
    regs = {reg} if reg else set((state.get("intake") or {}).get("candidate_regulators") or [])
    ctx_rel = (sum(e["regulator"] in regs for e in ev) / len(ev)) if ev else 0.0
    return {"groundedness": (v.get("scores") or {}).get("groundedness", 0.0),
            "faithfulness": (v.get("scores") or {}).get("citation_resolution", 0.0),
            "context_relevance": round(ctx_rel, 3),
            "hallucination": 0.0 if v.get("verified") or final["status"] != "completed" else 1.0}


async def process(job: dict) -> None:
    s = settings()
    run_id, cid = job["run_id"], job["correlation_id"]
    run_id_var.set(run_id)
    correlation_id_var.set(cid)
    started = time.time()
    RUNS_STARTED.inc()
    await store.update_run(run_id, status="running", started_at=started, worker=HOST)
    await store.add_step(run_id, {"agent": "worker", "node": "dequeued", "worker": HOST,
                                  "queue_wait_ms": round((started - job["submitted_at"]) * 1000, 1)})
    use_cache = job.get("use_cache", True)

    async def record(step: dict):
        await store.add_step(run_id, step)
        log.info("agent_step", **{k: v for k, v in step.items() if k not in ("passages",)})

    ctx = RunContext(retriever(), record, call_verifier,
                     cache_get=store.cache_get if use_cache else None,
                     cache_set=(lambda k, v: store.cache_set(k, v, s.retrieval_cache_ttl_s)) if use_cache else None)
    with tracer().start_as_current_span("agent_run", context=extract_trace_context(job.get("trace", {}))) as span:
        span.set_attribute("run_id", run_id)
        span.set_attribute("correlation_id", cid)
        try:
            state = await asyncio.wait_for(
                run_graph(ctx, run_id, cid, job["complaint"], job["variant"], job.get("demo_fault")),
                timeout=s.max_wall_clock_s + 30)
            final = state["final"]
        except Exception as e:  # never return an incomplete run as a confident answer
            log.exception("run_failed")
            state, final = {}, {"status": "failed", "determination": None,
                                "escalation": {"required": True, "reason": f"run failed: {type(e).__name__}",
                                               "path": ESCALATION_PATH}}
        span.set_attribute("status", final["status"])
    end = time.time()
    rec = await store.update_run(
        run_id, status=final["status"], determination=final.get("determination"), escalation=final.get("escalation"),
        verification={k: (state.get("verification") or {}).get(k) for k in ("verified", "failures", "scores",
                                                                             "llm_judge", "resolved_citations")},
        intake=state.get("intake"), guard=state.get("guard"), prompt_versions=state.get("prompt_refs", {}),
        usage=state.get("usage", {}),
        counters={"steps": state.get("steps"), "tool_calls": state.get("tool_calls"),
                  "verify_attempts": state.get("attempt"), "evidence_passages": len(state.get("evidence") or [])},
        timings={"queue_wait_s": round(started - job["submitted_at"], 3), "run_s": round(end - started, 3),
                 "end_to_end_s": round(end - job["submitted_at"], 3)},
        finished_at=end)
    RUN_DURATION.labels(final["status"]).observe(end - job["submitted_at"])
    RUNS_FINISHED.labels(final["status"], (final.get("determination") or {}).get("route", "none")).inc()
    STEPS_PER_RUN.observe(state.get("steps") or 0)
    TOOL_CALLS_PER_RUN.observe(state.get("tool_calls") or 0)
    if use_cache and final["status"] in ("completed", "escalated") and job.get("run_cache_key"):
        await store.cache_set(job["run_cache_key"], {"run_id": run_id}, s.run_cache_ttl_s)
    if state and random.random() < s.live_eval_sample_rate:
        sc = live_scores(final, state)
        for k, v in sc.items():
            LIVE_QUALITY.labels(k).observe(v)
            LIVE_QUALITY_LAST.labels(k).set(v)
        await store.add_quality({"ts": end, "run_id": run_id, **sc})
    store.persist_run_file(run_id, rec, await store.get_steps(run_id))
    log.info("run_finished", status=final["status"], end_to_end_s=rec["timings"]["end_to_end_s"])
    if job.get("callback_url"):
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(job["callback_url"], json={"run_id": run_id, "status": final["status"]},
                             headers={"X-Correlation-ID": cid})
        except httpx.HTTPError as e:
            log.warning("callback_failed", error=str(e)[:200])


async def loop(slot: int) -> None:
    while True:
        try:
            job = await store.dequeue(timeout=5)
            if job:
                await process(job)
            await store.r().set(f"worker:{HOST}", time.time(), ex=30)
        except Exception:
            log.exception("worker_loop_error", slot=slot)
            await asyncio.sleep(1)


async def main() -> None:
    setup_logging()
    setup_tracing("agent-worker")
    start_http_server(int(os.getenv("METRICS_PORT", "9100")))
    retriever()  # load model + index before taking work
    s = settings()
    log.info("worker_started", host=HOST, concurrency=s.worker_concurrency)
    await asyncio.gather(*(loop(i) for i in range(s.worker_concurrency)))


if __name__ == "__main__":
    asyncio.run(main())
