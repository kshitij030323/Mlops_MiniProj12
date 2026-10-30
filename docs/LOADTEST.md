# Load test (D6, A7, D7)

**Target (declared beforehand, `docs/SLO.md`):** p95 end-to-end run ≤ 30 s at ≥ 0.5 runs/s.

**Setup.** Locust (`loadtest/locustfile.py`) measures **submission → terminal result** as a custom `RUN complete_run` event. The LLM is the stub (1,200 ms ± 30 % per call, about 10 calls/run). `use_cache=false` unless stated, and each Locust user has its own client ID. Each step ran for 90 s, ramp 4 users/s, and each user waits 0.5–1.5 s between runs. `WORKER_CONCURRENCY=4` per worker.

Measured twice on the development laptop (Apple Silicon, 11 cores): first with the services as local processes, then under **Docker Compose** (Colima VM, 4 vCPU / 8 GB), with Locust itself running in a container (`docker compose --profile loadtest run --rm locust`). Raw CSVs: `loadtest/results/local_*` and `loadtest/results/compose_*`.

### Docker Compose (authoritative)

| Workers | Users | Completed runs | Throughput (runs/s) | p50 (s) | p95 (s) | Failures | SLO |
|---|---|---|---|---|---|---|---|
| 1 | 16 | 28 | 0.31 | 42 | **50** | 0 | ❌ |
| **3** (`--scale agent-worker=3`) | 16 | 78 | **0.87** | 16 | **21** | 0 | ✅ **met** |

### Local processes (same code, no containers)

| Workers | Users | Completed runs | Throughput (runs/s) | p50 (s) | p95 (s) | Failures | SLO (p95 ≤ 30 s, ≥ 0.5/s) |
|---|---|---|---|---|---|---|---|
| 1 | 4 | 23 | 0.27 | 14 | 15 | 0 | latency ✅, throughput ❌ |
| 1 | 8 | 26 | 0.29 | 24 | 29 | 0 | latency ✅ (barely), throughput ❌ |
| 1 | 16 | 25 | 0.28 | 46 | **55** | 0 | ❌ |
| **3** | 16 | 79 | **0.88** | 15 | **20** | 0 | ✅ **met** |
| 3 | 16 (cache on) | 687 | 7.69 | 0.01 | 13 | 0 | ✅ |

API submit latency stayed at p95 ≤ 29 ms throughout. The API is not the bottleneck.

## Where the target fails, and why

* **With one worker the target fails at 16 concurrent users**: p95 55 s. Latency is still inside the SLO at 8 users (p95 29 s), but throughput never exceeds about 0.29 runs/s.
* **Bottleneck: agent-worker execution slots, not CPU, Redis or the API.** One worker has 4 concurrent run slots, and a run holds a slot for about 13–14 s, almost all of it waiting on the model's 10 sequential calls. Capacity is therefore 4 / 13.5 s ≈ **0.30 runs/s**, which matches the measured 0.27–0.29 plateau. Beyond that, extra users only add queueing time: p50 rose from 14 s to 46 s while throughput stayed flat, and `run_queue_depth` grew.
* **`--scale agent-worker=3` helps almost linearly:** 3 × 0.30 = 0.90 expected, **0.88 measured**. p95 fell from 55 s to 20 s at the same 16 users, so the SLO was met. Scaling helps because the bottleneck is per-worker slots. Raising `WORKER_CONCURRENCY` would help the same way at a lower memory cost, since a run is I/O-bound. With a **real provider** the binding constraint becomes the provider's rate limit (tokens/requests per minute), and adding workers then makes things worse (429s and an open breaker). That is why rate limiting and caching are cost and capacity controls here.

## Effect of caching (D7)

Whole-run cache, keyed on the normalised complaint + prompt versions + index version: the 24-scenario mix repeats, so after the first pass almost every run is a hit.

| | p50 | p95 | Throughput | Model calls / cost per run |
|---|---|---|---|---|
| No cache (3 workers, 16 users) | 15 s | 20 s | 0.88 runs/s | 9.9 calls, $0.0073 |
| Run cache on | 0.01 s | 13 s (first misses only) | 7.7 runs/s | ≈ 0 for hits, so cost scales with the miss rate |

A cache hit returns in milliseconds with no model call. The p95 of 13 s comes from the cold misses at the start of the test. Retrieval results are also cached (Redis, keyed by query + filters + index version), and query embeddings use an in-process LRU. Those save ~10–30 ms per tool call, which is small next to model latency. They mainly matter when the stub is replaced by a fast local model.

Justification: investors commonly resubmit the same grievance, and a cached answer is identical by construction, because the key includes the prompt and index versions. Degraded, failed and demo-fault runs are **never** cached.
