# Runbook: the two most likely failure modes

## 1. Model provider unavailable or rate-limited (circuit breaker open)

Most likely in practice: free tiers return 429 under low limits, and providers have outages.

**Symptom**
* Runs come back `status: degraded`, with `escalation.reason` starting "Degraded mode: the model provider was unavailable".
* Grafana: *Circuit breaker open* = 1 on one or more workers. *Error rate (runs)* rises. *LLM retry rate* spikes.
* Worker logs show `"event": "llm_call_failed"` and then `"event": "circuit_breaker_opened"`.

**Confirm**
```bash
curl -s localhost:9090/api/v1/query --data-urlencode 'query=llm_circuit_breaker_open' | python3 -m json.tool
docker compose logs agent-worker --since 5m | grep -E 'llm_call_failed|circuit_breaker'
curl -s localhost:8082/health        # verifier breaker snapshot
# For the stub:  curl -s localhost:8090/health  (mode should be "ok")
# For a real provider: check the HTTP status in llm_call_failed (429 = rate limit, 401 = key, 5xx = outage)
```

**Recover**
1. Fix the cause. 429: reduce load (lower `RATE_LIMIT_PER_MIN`, scale workers *down*, enable caching), or wait for the quota window. 401: correct `LLM_API_KEY` in `.env`, then `docker compose up -d`. Outage: switch provider by changing `LLM_BASE_URL`/`LLM_MODEL` in `.env`, then `docker compose up -d agent-worker verifier`.
2. No manual reset is needed. After `BREAKER_RESET_S` (30 s) the breaker goes half-open. One successful call closes it.
3. Degraded runs were escalated, so none were issued as answers. Officers handle them from the queue, or the client resubmits once healthy.

## 2. Run backlog / runs stuck in `queued` or `running` (workers down, saturated or crash-looping)

**Symptom**
* Clients poll indefinitely. Runs flip to `failed` with "run exceeded deadline without completing" after `RUN_DEADLINE_S`.
* Grafana: *Queue depth* climbing, *Run latency p95* above the 30 s SLO line, throughput flat.
* `/health` on the API shows `workers_alive: 0` (or fewer than expected).

**Confirm**
```bash
curl -s localhost:8000/health | python3 -m json.tool          # workers_alive, redis, index
docker compose ps agent-worker                               # restarting? unhealthy?
docker compose logs agent-worker --tail 100                  # e.g. index missing, OOM, model load errors
docker compose exec redis redis-cli llen queue:runs          # backlog size
```
Common causes: the index is missing or was built with another embedding model ("index was built with a different embedding model"); the container ran out of memory (exit 137); a slow provider is holding all `WORKER_CONCURRENCY` slots.

**Recover**
1. Index problem: `docker compose run --rm indexer python -m grievance.ingest build --reembed`, then `docker compose restart agent-worker`.
2. Saturation: `docker compose up -d --scale agent-worker=3` (or raise `WORKER_CONCURRENCY` if the bottleneck is model wait, not CPU). Watch queue depth drain.
3. Crash loop or OOM: raise Docker Desktop memory (≥ 6 GB) and restart the worker.
4. Runs that already reported `failed` are not retried automatically. Resubmit them (identical requests hit the run cache if an earlier run completed).
