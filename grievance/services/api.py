"""Public REST API (F1, F2, A4, D1, D7, L6).

Asynchronous by design: POST /v1/complaints validates at the boundary and returns a run_id immediately
(202); the caller polls GET /v1/runs/{run_id} or supplies callback_url. Runs take tens of seconds with a real
provider, so holding an HTTP connection open would tie up API workers and break behind proxies with ~30-60 s
idle timeouts. A Redis queue sits between submission and execution so agent workers scale independently.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .. import prompts, store
from ..config import settings
from ..contracts import CONTRACT_VERSION, ComplaintRequest, Feedback, RunAccepted, RunResult
from ..observability import (FEEDBACK, HTTP_LATENCY, HTTP_REQUESTS, QUARANTINED, QUEUE_DEPTH, RATE_LIMITED,
                             RUN_CACHE, RUNS_SUBMITTED, correlation_id_var, inject_trace_context, log, setup_logging,
                             setup_tracing, INDEX_BUILT_TS, INDEX_CHUNKS, INDEX_DOCS, INGEST_THROUGHPUT,
                             INGEST_EMBEDDED)
from ..retrieval import cache_key

setup_logging()
app = FastAPI(title="Securities Investor Grievance Routing Service", version=CONTRACT_VERSION,
              description="Routes synthetic investor complaints with cited justification. Never disposes of a "
                          "complaint; a grievance officer decides.")
setup_tracing("api", app)
STARTED = time.time()
WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"
TERMINAL = {"completed", "escalated", "degraded", "failed"}


@app.middleware("http")
async def correlation(request: Request, call_next):
    cid = request.headers.get("X-Correlation-ID") or uuid.uuid4().hex
    correlation_id_var.set(cid)
    request.state.cid = cid
    t0 = time.perf_counter()
    resp = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", "unmatched")
    HTTP_REQUESTS.labels("api", path, request.method, str(resp.status_code)).inc()
    HTTP_LATENCY.labels("api", path).observe(time.perf_counter() - t0)
    resp.headers["X-Correlation-ID"] = cid
    return resp


@app.exception_handler(RequestValidationError)
async def quarantine_rejects(request: Request, exc: RequestValidationError):
    """D1: malformed requests are quarantined with field-level errors; no run is created or enqueued."""
    errors = [{"field": ".".join(str(p) for p in e["loc"][1:]) or "body", "type": e["type"], "message": e["msg"]}
              for e in exc.errors()]
    rec = {"quarantine_id": uuid.uuid4().hex, "received_at": time.time(), "path": request.url.path,
           "correlation_id": request.state.cid, "client": _client(request), "errors": errors,
           "body": jsonable_encoder(exc.body) if exc.body is not None else None, "agent_run_started": False}
    if request.url.path == "/v1/complaints":
        await store.quarantine(rec)
        for e in errors:
            QUARANTINED.labels(e["type"]).inc()
        log.warning("request_quarantined", errors=errors)
    return JSONResponse(status_code=422, content={"detail": "request rejected at ingestion boundary",
                                                  "quarantine_id": rec["quarantine_id"], "errors": errors,
                                                  "agent_run_started": False})


def _client(request: Request) -> str:
    return request.headers.get("X-Client-Id") or (request.client.host if request.client else "unknown")


def _manifest() -> dict:
    p = Path(settings().index_dir) / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _run_cache_key(c: ComplaintRequest, variant: str) -> str:
    fields = c.model_dump(mode="json", exclude={"complainant", "client_reference", "callback_url", "options"})
    fields["complaint_text"] = " ".join(fields["complaint_text"].lower().split())
    refs = [prompts.load(t, variant)["ref"] for t in ("intake", "determine")] + [prompts.load("planner", "v1")["ref"]]
    return "run:" + cache_key(fields, variant, refs, _manifest().get("index_version"))


@app.post("/v1/complaints", status_code=202, response_model=RunAccepted)
async def submit(payload: ComplaintRequest, request: Request):
    s = settings()
    client = _client(request)
    ok, n = await store.rate_limit_ok(client, s.rate_limit_per_min)
    if not ok:
        RATE_LIMITED.inc()
        raise HTTPException(429, f"rate limit of {s.rate_limit_per_min} runs/min exceeded for client '{client}'",
                            headers={"Retry-After": "60"})
    cid = request.state.cid
    run_id = uuid.uuid4().hex[:16]
    variant = payload.options.prompt_variant or s.prompt_variant
    now = time.time()
    rkey = _run_cache_key(payload, variant)
    if payload.options.use_cache and payload.options.demo_fault is None:
        hit = await store.cache_get(rkey)
        prev = await store.get_run(hit["run_id"]) if hit else None
        if prev and prev.get("status") in ("completed", "escalated"):
            RUN_CACHE.labels("hit").inc()
            rec = {**prev, "run_id": run_id, "correlation_id": cid, "cached_from": prev["run_id"],
                   "submitted_at": now, "timings": {**prev.get("timings", {}), "end_to_end_s": 0.0}}
            await store.save_run(run_id, rec)
            await store.add_step(run_id, {"agent": "api", "node": "run_cache_hit", "cached_from": prev["run_id"]})
            return RunAccepted(run_id=run_id, status=rec["status"], status_url=f"/v1/runs/{run_id}",
                               correlation_id=cid, cached=True)
        RUN_CACHE.labels("miss").inc()
    complaint = payload.model_dump(mode="json")
    await store.save_run(run_id, {"run_id": run_id, "status": "queued", "correlation_id": cid, "client": client,
                                  "submitted_at": now, "variant": variant, "contract_version": CONTRACT_VERSION})
    await store.add_step(run_id, {"agent": "api", "node": "accepted", "client": client, "variant": variant})
    await store.enqueue({"run_id": run_id, "correlation_id": cid, "complaint": complaint, "variant": variant,
                         "use_cache": payload.options.use_cache, "demo_fault": payload.options.demo_fault,
                         "callback_url": str(payload.callback_url) if payload.callback_url else None,
                         "submitted_at": now, "run_cache_key": rkey, "trace": inject_trace_context()})
    RUNS_SUBMITTED.inc()
    QUEUE_DEPTH.set(await store.queue_depth())
    log.info("run_submitted", run_id=run_id, client=client, variant=variant)
    return RunAccepted(run_id=run_id, status="queued", status_url=f"/v1/runs/{run_id}", correlation_id=cid)


@app.get("/v1/runs/{run_id}", response_model=RunResult, response_model_exclude_none=True)
async def get_run(run_id: str):
    rec = await store.get_run(run_id)
    if not rec:
        f = store.read_run_file(run_id)
        rec = f["run"] if f else None
    if not rec:
        raise HTTPException(404, "unknown run_id")
    if rec["status"] not in TERMINAL and time.time() - rec["submitted_at"] > settings().run_deadline_s:
        # A worker died mid-run (or the queue is stuck): report failure, never a partial answer.
        rec = await store.update_run(run_id, status="failed", escalation={
            "required": True, "reason": "run exceeded deadline without completing (worker failure or backlog)",
            "path": "Grievance officer queue (manual triage)"})
    return RunResult(**{k: v for k, v in rec.items() if k in RunResult.model_fields})


@app.get("/v1/runs/{run_id}/trace")
async def get_trace(run_id: str):
    """Everything needed to reconstruct a run: tool calls, queries, passages, prompt versions, per-step latency,
    tokens and cost, verification outcome."""
    rec, steps = await store.get_run(run_id), await store.get_steps(run_id)
    if not rec:
        f = store.read_run_file(run_id)
        if not f:
            raise HTTPException(404, "unknown run_id")
        rec, steps = f["run"], f["steps"]
    per_step_cost = [{"node": s.get("node"), "agent": s.get("agent"), "latency_ms": s.get("latency_ms"),
                      "prompt_tokens": s.get("prompt_tokens", 0), "completion_tokens": s.get("completion_tokens", 0),
                      "cost_usd": s.get("cost_usd", 0.0)} for s in steps]
    return {"run": rec, "steps": steps, "per_step_cost": per_step_cost}


@app.post("/v1/runs/{run_id}/feedback", status_code=201)
async def feedback(run_id: str, fb: Feedback):
    if not await store.get_run(run_id):
        raise HTTPException(404, "unknown run_id")
    await store.add_feedback(run_id, {**fb.model_dump(), "ts": time.time()})
    FEEDBACK.labels(str(fb.satisfactory).lower()).inc()
    return {"stored": True}


@app.get("/v1/feedback/summary")
async def feedback_summary():
    return await store.feedback_summary()


@app.get("/v1/quality")
async def quality(n: int = 200):
    return {"samples": await store.quality_series(n)}


@app.get("/v1/quarantine")
async def quarantine(n: int = 20):
    return {"items": await store.list_quarantine(n)}


@app.get("/v1/contracts/complaint")
async def contract():
    return ComplaintRequest.model_json_schema()


@app.get("/health")
async def health():
    checks = {}
    try:
        checks["redis"] = bool(await store.r().ping())
    except Exception as e:  # noqa: BLE001
        checks["redis"] = False
        checks["redis_error"] = str(e)[:100]
    m = _manifest()
    checks["index"] = bool(m)
    workers = await store.r().keys("worker:*") if checks["redis"] else []
    body = {"status": "ok" if checks["redis"] and checks["index"] else "degraded", "checks": checks,
            "workers_alive": len(workers), "uptime_s": round(time.time() - STARTED),
            "index": {k: m.get(k) for k in ("built_at", "index_version", "n_chunks", "embedding_model", "chunking")}}
    return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)


@app.get("/metrics")
async def metrics():
    try:
        QUEUE_DEPTH.set(await store.queue_depth())
        m = _manifest()
        if m:
            import datetime as dt
            INDEX_BUILT_TS.set(dt.datetime.fromisoformat(m["built_at"]).timestamp())
            INDEX_CHUNKS.set(m["n_chunks"])
            INDEX_DOCS.set(len(m["documents"]))
            INGEST_THROUGHPUT.set(m["throughput_chunks_per_s"])
            INGEST_EMBEDDED.set(m["n_embedded_this_build"])
    except Exception:  # noqa: BLE001 - metrics must still render
        pass
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/")
async def web_client():
    return FileResponse(WEB)
