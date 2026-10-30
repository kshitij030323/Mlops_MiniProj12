"""Structured logging, correlation IDs, Prometheus metrics and OpenTelemetry tracing (F8, D5, A6)."""
from __future__ import annotations

import contextvars
import logging
import os
import sys

import structlog
from prometheus_client import Counter, Gauge, Histogram

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")
run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")


def _add_context(_, __, event_dict):
    event_dict.setdefault("correlation_id", correlation_id_var.get())
    event_dict.setdefault("run_id", run_id_var.get())
    event_dict.setdefault("service", os.getenv("SERVICE_NAME", "grievance"))
    return event_dict


def setup_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=os.getenv("LOG_LEVEL", "INFO"))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


log = structlog.get_logger()

# ---------------------------------------------------------------- tracing
_tracer = None


def setup_tracing(service_name: str, app=None) -> None:
    global _tracer
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,health")


def tracer():
    from opentelemetry import trace
    return _tracer or trace.get_tracer("grievance")


def inject_trace_context() -> dict:
    from opentelemetry.propagate import inject
    carrier: dict = {}
    inject(carrier)
    return carrier


def extract_trace_context(carrier: dict):
    from opentelemetry.propagate import extract
    return extract(carrier or {})


# ---------------------------------------------------------------- metrics
LAT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 6, 8, 10, 15, 20, 30, 45, 60, 90, 120, 180)

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "route", "method", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP latency", ["service", "route"], buckets=LAT_BUCKETS)

RUNS_SUBMITTED = Counter("runs_submitted_total", "Runs accepted and enqueued")
RUNS_STARTED = Counter("runs_started_total", "Agent runs started by a worker")
RUNS_FINISHED = Counter("runs_finished_total", "Agent runs finished", ["status", "route"])
RUN_DURATION = Histogram("run_duration_seconds", "End-to-end run latency, submission to final result",
                         ["status"], buckets=LAT_BUCKETS)
RUN_CACHE = Counter("run_cache_total", "Whole-run cache lookups", ["result"])
RETRIEVAL_CACHE = Counter("retrieval_cache_total", "Retrieval cache lookups", ["result"])
QUARANTINED = Counter("quarantined_requests_total", "Requests rejected at the ingestion boundary", ["reason"])
RATE_LIMITED = Counter("rate_limited_total", "Requests rejected by per-client rate limit")
QUEUE_DEPTH = Gauge("run_queue_depth", "Runs waiting in the queue")

AGENT_TASKS = Counter("agent_tasks_total", "Agent task outcomes", ["agent", "outcome"])
AGENT_STEP_LATENCY = Histogram("agent_step_duration_seconds", "Per-step latency", ["agent", "step"], buckets=LAT_BUCKETS)
STEPS_PER_RUN = Histogram("agent_steps_per_run", "Graph steps per run", buckets=(1, 2, 4, 6, 8, 10, 12, 14, 16, 20))
TOOL_CALLS_PER_RUN = Histogram("agent_tool_calls_per_run", "Tool calls per run", buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10))
TOOL_CALLS = Counter("agent_tool_calls_total", "Tool calls", ["agent", "tool", "outcome"])
VERIFY_OUTCOMES = Counter("verification_total", "Verification outcomes", ["outcome"])

LLM_CALLS = Counter("llm_calls_total", "LLM calls", ["agent", "step", "outcome"])
LLM_RETRIES = Counter("llm_retries_total", "LLM retry attempts", ["agent", "step"])
LLM_LATENCY = Histogram("llm_call_duration_seconds", "LLM call latency", ["step"], buckets=LAT_BUCKETS)
LLM_TOKENS = Counter("llm_tokens_total", "Tokens", ["agent", "step", "kind"])
LLM_COST = Counter("llm_cost_usd_total", "LLM cost in USD at call time", ["agent", "step"])
BREAKER_STATE = Gauge("llm_circuit_breaker_open", "1 when the provider circuit breaker is open", ["breaker"])
DEGRADED_RUNS = Counter("degraded_runs_total", "Runs that fell back to the degradation path")

GUARDRAIL_EVENTS = Counter("guardrail_events_total", "Guardrail detections", ["kind", "stage"])
FEEDBACK = Counter("feedback_total", "Caller feedback", ["satisfactory"])
LIVE_QUALITY = Histogram("live_eval_score", "Automatic quality scores of sampled live runs", ["metric"],
                         buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0))
LIVE_QUALITY_LAST = Gauge("live_eval_last_score", "Last automatic quality score", ["metric"])

INDEX_BUILT_TS = Gauge("index_built_timestamp_seconds", "When the vector index was built")
INDEX_CHUNKS = Gauge("index_chunks", "Chunks in the vector index")
INDEX_DOCS = Gauge("index_documents", "Documents in the vector index")
INGEST_THROUGHPUT = Gauge("ingest_throughput_chunks_per_second", "Ingestion pipeline throughput (last build)")
INGEST_EMBEDDED = Gauge("ingest_chunks_embedded_last_build", "Chunks actually (re-)embedded on the last build")
