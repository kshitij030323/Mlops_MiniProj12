"""Verifier agent service (A2/A3). Separate process and container from the research agent:
independent release of the verification policy, and a failure here cannot produce an unverified answer
(the worker escalates when this service is unreachable)."""
from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .. import llm
from ..agents.verifier_agent import run_verifier
from ..observability import (AGENT_STEP_LATENCY, HTTP_LATENCY, HTTP_REQUESTS, correlation_id_var, log, setup_logging,
                             setup_tracing)

setup_logging()
app = FastAPI(title="Verifier agent", version="1.0.0")
setup_tracing("verifier", app)
STARTED = time.time()


@app.post("/verify")
async def verify(req: Request, payload: dict):
    cid = req.headers.get("X-Correlation-ID", "-")
    correlation_id_var.set(cid)
    t0 = time.perf_counter()
    res = await run_verifier(payload, cid)
    dur = time.perf_counter() - t0
    AGENT_STEP_LATENCY.labels("verifier", "verify_service").observe(dur)
    HTTP_LATENCY.labels("verifier", "/verify").observe(dur)
    HTTP_REQUESTS.labels("verifier", "/verify", "POST", "200").inc()
    log.info("verification", verified=res["verified"], failures=[f["code"] for f in res["failures"]],
             llm_judge=res.get("llm_judge"))
    return res


@app.get("/health")
async def health():
    return {"status": "ok", "uptime_s": round(time.time() - STARTED), "breaker": llm.breaker().snapshot()}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
