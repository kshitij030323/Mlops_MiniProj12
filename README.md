# Securities Investor Grievance Routing Service

**Mini Project 12 · UE24AM342AA5 ML System Design and AgentOps · PES University**

| Team member | SRN |
|---|---|
| Kshitij Gupta | PES1UG24AM915 |
| Vinay Rampur | PES1UG24AM455 |
| Vikas R | PES1UG24AM477 |

A multi-agent service that takes a (synthetic) investor complaint and:

1. validates it at the boundary and quarantines rejects;
2. works out the **product → entity → regulator** chain;
3. retrieves the jurisdictional and servicing provisions;
4. issues a **routing recommendation** with circular-level citations: `assign_to_entity` with a timeline, `refer_to_other_regulator`, or `not_maintainable` with a reason;
5. **escalates to a human** when the complaint splits across regulators, the entity type is outside the corpus, or the answer cannot be verified.

The system never disposes of a complaint. A grievance officer decides.

* Full write-up: [`REPORT.md`](REPORT.md)
* Architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
* Runbook: [`docs/RUNBOOK.md`](docs/RUNBOOK.md)
* Incident log: [`docs/INCIDENT_LOG.md`](docs/INCIDENT_LOG.md)

---

## 1. Run it (clean clone → working system)

Prerequisites: Docker with Compose v2.20+ and buildx (Docker Desktop, or on macOS without Desktop: `brew install docker docker-compose docker-buildx colima && colima start --cpu 4 --memory 8`), about 8 GB free disk, and internet access for the first build. The build downloads the embedding model and bakes it into the image.

```bash
git clone <this repo> && cd <repo>
cp .env.example .env                 # defaults use the local LLM stub, so no API key is needed
docker compose up --build -d         # first build takes ~5-10 min
docker compose ps                    # wait until api, agent-worker, verifier and llm-stub are "healthy"
```

| What | URL |
|---|---|
| Web client (A8) | http://localhost:8000/ |
| API docs (OpenAPI) | http://localhost:8000/docs |
| Grafana dashboard (D5/A6) | http://localhost:3000 (login from `.env`) → *Grievance Routing* |
| Jaeger traces | http://localhost:16686 (services `api`, `agent-worker`, `verifier`) |
| Prometheus | http://localhost:9090 |

**Slow network?** If `pip install` times out during the build, download the wheels on the host (usually much faster than inside a Docker VM) and pass them in as a build context:

```bash
pip download -r requirements.txt -d ~/wheelhouse --only-binary=:all: --python-version 3.12 \
  --implementation cp --abi cp312 --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 \
  -c <(echo torch==2.5.1)          # use x86_64 platform tags on Intel/AMD machines
WHEELHOUSE_DIR=~/wheelhouse docker compose build indexer && docker compose up -d
```

(`HF_SEED_DIR` can likewise point at a directory holding a pre-downloaded `models--sentence-transformers--all-MiniLM-L6-v2` Hugging Face cache.)

Nothing runs outside the containers. The one-shot `indexer` container builds the FAISS index into the shared `data` volume. `api` and `agent-worker` wait for it to finish.

### Use a real model instead of the stub

Edit `.env` and set `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` for any OpenAI-compatible provider (Groq or Gemini free tier, OpenRouter, or a local Ollama). Then run `docker compose up -d`. **Set a spending limit on the provider account first.** No code changes are needed: the provider sits behind `grievance/llm.py`.

### Scale the agent workers (A7)

```bash
docker compose up -d --scale agent-worker=3
```

## 2. Try it

```bash
scripts/demo_requests.sh hard          # section 4.1 case -> refer_to_other_regulator (RBI) with citations
scripts/demo_requests.sh poll <run_id> # poll result
scripts/demo_requests.sh trace <run_id># full reconstruction: tool calls, queries, passages, prompts, latency, cost
```

Or with curl:

```bash
curl -s -X POST localhost:8000/v1/complaints -H 'Content-Type: application/json' -H 'X-Client-Id: me' -d '{
  "complainant": {"name": "Synthetic Investor", "email": "s@example.com", "state": "Karnataka"},
  "channel": "web",
  "complaint_text": "I invested in a fixed deposit through my bank'"'"'s app. The interest credited is lower than what I was promised. I want to complain to SEBI.",
  "entity_name": "Example Bank Ltd", "incident_date": "2026-10-01", "regulator_addressed": "SEBI"}'
# -> 202 {"run_id": "...", "status": "queued", "status_url": "/v1/runs/<id>"}
curl -s localhost:8000/v1/runs/<run_id>
```

### API (data contracts in `contracts/`, served at `/v1/contracts/complaint`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/complaints` | Submit a complaint. Returns **202 + run_id** at once. Malformed input gets 422, is quarantined, and no run starts |
| GET | `/v1/runs/{run_id}` | Poll status and result (`queued/running/completed/escalated/degraded/failed`) |
| GET | `/v1/runs/{run_id}/trace` | Full run reconstruction |
| POST | `/v1/runs/{run_id}/feedback` | `{"satisfactory": bool, "reason": str}` |
| GET | `/v1/feedback/summary` | Aggregated feedback |
| GET | `/v1/quality` | Automatic quality scores of sampled live runs |
| GET | `/v1/quarantine` | Rejected requests with field-level errors |
| GET | `/health`, `/metrics` | Health and Prometheus metrics (every service exposes both) |

The caller may pass an optional `callback_url`. The worker POSTs `{run_id, status}` to it when the run finishes.

## 3. Demonstration checklist (section 9)

| # | Item | How |
|---|---|---|
| 1 | Clean clone → working system | Section 1 above |
| 2 | Valid request → routed recommendation with resolving citations | `scripts/demo_requests.sh valid`, then `poll`. Every citation has `passage_id` and a verbatim `quote`, and `verification.resolved_citations` lists them |
| 3 | One run traced end to end | `scripts/demo_requests.sh trace <id>` and Jaeger (search by `run_id` tag) |
| 4 | Malformed request quarantined, no run started | `scripts/demo_requests.sh malformed`. The response has `"agent_run_started": false`. `GET /v1/quarantine` shows it. `runs_submitted_total` / `runs_started_total` on `/metrics` do not change |
| 5 | Unanswerable case → escalation | `scripts/demo_requests.sh outofcorpus` (insurance → IRDAI, not in the corpus) |
| 6 | Self-verification catches an unsupported answer | `scripts/demo_requests.sh fault` injects a fabricated citation into the first draft (only when `DEMO_FAULTS_ENABLED=true`). The trace shows `verify: citation_not_retrieved`, then `get_clause` re-search, then a clean redraft that verifies |
| 7 | Prompt injection → guardrail | `scripts/demo_requests.sh injection` gets escalated by the input guard before any model holding tools is called |
| 8 | Circuit breaker + degradation | `curl -X POST localhost:8090/admin/mode -d '{"mode":"down"}' -H 'Content-Type: application/json'` (or `docker compose stop llm-stub`), then submit. You see retries with backoff, the breaker opens (`llm_circuit_breaker_open=1`), and the run returns **degraded** with a provisional route, escalated. Restore with `{"mode":"ok"}` |
| 9 | Locust vs target, failure concurrency, effect of `--scale` | `docker compose --profile loadtest run --rm locust` (see `docs/LOADTEST.md`) |
| 10 | Section 4.1 case | `scripts/demo_requests.sh hard` (expected answer in `docs/EVALUATION.md`) |

## 4. Evaluation, ingestion, tests

```bash
# Evaluation set (24 scenarios incl. section 4.1), batch from the CLI, inside the containers:
docker compose exec agent-worker python -m eval.run_eval --tag clause_v2
docker compose exec agent-worker python -m eval.run_eval --variant v1 --tag clause_v1      # prompt A/B (L2)
docker compose exec agent-worker python -m grievance.ingest build --chunking fixed --out /data/index_fixed
docker compose exec agent-worker python -m eval.run_eval --index-dir /data/index_fixed --tag fixed_v2   # deliberate change (L3)

# Ingestion pipeline (idempotent; incremental re-embedding)
docker compose exec agent-worker python -m grievance.ingest build      # only changed chunks are embedded
docker compose exec agent-worker python -m grievance.ingest status

# Copy evaluation results out of the container
docker compose cp agent-worker:/app/eval/results ./eval/

# Unit tests
docker compose exec agent-worker python -m pytest -q tests
```

## 5. Repository layout

```
grievance/                 python package (one image, five services)
  contracts.py             data contracts (pydantic) -> contracts/*.schema.json
  agents/graph.py          LangGraph: input guard, intake agent, research agent loop, draft, verify, finalize
  agents/tools.py          read-only tools: search_corpus, get_clause, get_complaint_field, list_regulator_scope
  agents/verifier_agent.py verifier agent (runs in the verifier service)
  verification.py          deterministic citation and grounding checks (shared with eval)
  rules.py                 domain rules: stub responses, degradation path, intake priors
  llm.py                   provider interface: timeouts, retry+backoff, circuit breaker, token/cost accounting
  guardrails.py            injection detection, sanitisation, toxicity, output filter
  ingest.py / retrieval.py corpus pipeline (FAISS via LangChain) and retrieval
  prompts/<task>/vN.yaml   versioned prompts
  services/                api, worker, verifier, llm_stub
  web/index.html           web client
corpus/raw/                clause-level corpus (see corpus/SOURCES.md)
eval/                      scenarios.jsonl (24), run_eval.py, results/
loadtest/                  locustfile.py, results/
ops/                       prometheus, grafana provisioning + dashboard
docs/                      ARCHITECTURE, SLO, EVALUATION, LOADTEST, RUNBOOK, INCIDENT_LOG
```
