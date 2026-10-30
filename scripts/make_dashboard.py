"""Generates ops/grafana/dashboards/grievance.json (kept as code so panels are reviewable)."""
import json
from pathlib import Path

P = []
def panel(title, exprs, unit="short", w=8, kind="timeseries", legend=None):
    P.append({"title": title, "type": kind, "datasource": {"type": "prometheus", "uid": "prom"},
              "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
              "targets": [{"expr": e, "legendFormat": (legend[i] if legend else ""), "refId": chr(65 + i)}
                          for i, e in enumerate(exprs)], "_w": w})

def row(title):
    P.append({"type": "row", "title": title, "_w": 24, "collapsed": False, "panels": []})

R = "[2m]"
row("Service (D5, D6)")
panel("Run latency end-to-end p50 / p95 (s)", [
    f"histogram_quantile(0.5, sum by (le) (rate(run_duration_seconds_bucket{R})))",
    f"histogram_quantile(0.95, sum by (le) (rate(run_duration_seconds_bucket{R})))", "30"], "s",
    legend=["p50", "p95", "SLO p95"])
panel("Throughput (runs finished/s) by status", [f"sum by (status) (rate(runs_finished_total{R}))"], "reqps",
      legend=["{{status}}"])
panel("Error rate (failed+degraded runs / all; HTTP 5xx / all)", [
    f'sum(rate(runs_finished_total{{status=~"failed|degraded"}}{R})) / clamp_min(sum(rate(runs_finished_total{R})), 1e-9)',
    f'sum(rate(http_requests_total{{status=~"5.."}}{R})) / clamp_min(sum(rate(http_requests_total{R})), 1e-9)'],
    "percentunit", legend=["runs", "http"])
panel("Availability (scrape up) by service", ["avg by (job) (up)"], "percentunit", legend=["{{job}}"])
panel("Queue depth", ["max(run_queue_depth)"], legend=["queued runs"])
panel("API latency p95 by route", [f"histogram_quantile(0.95, sum by (le, route) (rate(http_request_duration_seconds_bucket{R})))"],
      "s", legend=["{{route}}"])
row("Agents (A6)")
panel("Task success rate by agent", [
    f'sum by (agent) (rate(agent_tasks_total{{outcome=~"ok|completed|escalated|blocked"}}{R})) / clamp_min(sum by (agent) (rate(agent_tasks_total{R})), 1e-9)'],
    "percentunit", legend=["{{agent}}"])
panel("Steps and tool calls per run (mean)", [
    f"rate(agent_steps_per_run_sum{R}) / clamp_min(rate(agent_steps_per_run_count{R}), 1e-9)",
    f"rate(agent_tool_calls_per_run_sum{R}) / clamp_min(rate(agent_tool_calls_per_run_count{R}), 1e-9)"],
    legend=["steps/run", "tool calls/run"])
panel("Per-step latency p95 by agent/step", [
    f"histogram_quantile(0.95, sum by (le, agent, step) (rate(agent_step_duration_seconds_bucket{R})))"], "s",
    legend=["{{agent}}/{{step}}"])
panel("LLM retry rate by agent", [
    f"sum by (agent) (rate(llm_retries_total{R})) / clamp_min(sum by (agent) (rate(llm_calls_total{R})), 1e-9)"],
    "percentunit", legend=["{{agent}}"])
panel("Circuit breaker open (per worker) / degraded runs", ["llm_circuit_breaker_open", f"sum(rate(degraded_runs_total{R}))"],
      legend=["{{instance}}", "degraded/s"])
panel("Verification outcomes", [f"sum by (outcome) (rate(verification_total{R}))"], "reqps", legend=["{{outcome}}"])
row("Containers")
panel("Container uptime (s)", ["time() - process_start_time_seconds"], "s", legend=["{{job}} {{instance}}"])
panel("CPU by container", ['sum by (name) (rate(container_cpu_usage_seconds_total{name=~".+"}[1m]))'], "percentunit",
      legend=["{{name}}"])
panel("Memory by container", ['sum by (name) (container_memory_working_set_bytes{name=~".+"})'], "bytes",
      legend=["{{name}}"])
row("LLMOps (L5, L6, D2, D7)")
panel("Cost per run (USD) and by step", [
    f"sum(rate(llm_cost_usd_total{R})) / clamp_min(sum(rate(runs_finished_total{R})), 1e-9)",
    f"sum by (step) (rate(llm_cost_usd_total{R})) / clamp_min(sum(rate(runs_finished_total{R})), 1e-9)"],
    "currencyUSD", legend=["per run", "{{step}}"])
panel("Tokens/s by step and kind", [f"sum by (step, kind) (rate(llm_tokens_total{R}))"], legend=["{{step}} {{kind}}"])
panel("Live quality (sampled runs)", ["live_eval_last_score"], "percentunit", legend=["{{metric}}"])
panel("Feedback satisfaction", ['sum(feedback_total{satisfactory="true"}) / clamp_min(sum(feedback_total), 1)'],
      "percentunit", kind="stat")
panel("Cache hit ratio (run / retrieval)", [
    f'sum(rate(run_cache_total{{result="hit"}}{R})) / clamp_min(sum(rate(run_cache_total{R})), 1e-9)',
    f'sum(rate(retrieval_cache_total{{result="hit"}}{R})) / clamp_min(sum(rate(retrieval_cache_total{R})), 1e-9)'],
    "percentunit", legend=["run", "retrieval"])
panel("Guardrail events / quarantined / rate-limited", [
    f"sum by (kind) (increase(guardrail_events_total[10m]))", "sum(increase(quarantined_requests_total[10m]))",
    "sum(increase(rate_limited_total[10m]))"], legend=["{{kind}}", "quarantined", "rate limited"])
panel("Index freshness (age, s) / chunks / ingest throughput", [
    "time() - max(index_built_timestamp_seconds)", "max(index_chunks)", "max(ingest_throughput_chunks_per_second)"],
    legend=["age s", "chunks", "chunks/s"])

x = y = 0
for i, p in enumerate(P):
    w = p.pop("_w")
    h = 1 if p["type"] == "row" else 8
    if x + w > 24 or p["type"] == "row":
        x, y = 0, y + (8 if x else 0)
    p["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
    p["id"] = i + 1
    if p["type"] == "row":
        y += 1
    else:
        x += w
dash = {"uid": "grievance", "title": "Grievance Routing Service", "schemaVersion": 39, "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"}, "panels": P, "tags": ["grievance"]}
out = Path(__file__).resolve().parent.parent / "ops" / "grafana" / "dashboards" / "grievance.json"
out.write_text(json.dumps(dash, indent=1))
print(f"wrote {out} ({len(P)} panels)")
