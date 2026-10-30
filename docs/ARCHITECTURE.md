# Architecture

## Deployment view (boundaries = containers)

```
                         ┌──────────────────────────── docker compose ─────────────────────────────┐
  browser / client       │                                                                          │
  ───────────────► :8000 │  ┌──────────┐  LPUSH job   ┌─────────┐  BRPOP   ┌────────────────────┐  │
   POST /v1/complaints   │  │   api    │─────────────►│  redis  │◄─────────│  agent-worker × N  │  │
   GET  /v1/runs/{id}    │  │ FastAPI  │◄─────────────│ queue + │  state   │ LangGraph agents   │  │
   (202 + poll/callback) │  │ contract │ run records  │ runs +  │─────────►│ intake + research  │  │
                         │  │ quarantn │ cache, rate  │ caches  │          │ FAISS + MiniLM     │  │
                         │  └──────────┘ limits       └─────────┘          └───┬──────────┬─────┘  │
                         │       ▲                                    HTTP     │          │ HTTP   │
                         │       │ /data/index (manifest)       /verify (A2)   ▼          ▼        │
                         │  ┌──────────┐ one-shot   ┌──────────────┐   ┌───────────┐ ┌──────────┐ │
                         │  │ indexer  │──────────► │ volume: data │   │ verifier  │ │ llm-stub │ │
                         │  │ ingest   │  FAISS     │ index, runs, │   │ agent     │─►│ (or real │ │
                         │  └──────────┘            │ quarantine   │   └───────────┘ │ provider)│ │
                         │                          └──────────────┘                 └──────────┘ │
                         │  prometheus ◄─ scrapes every service   grafana ◄─ prometheus, jaeger   │
                         │  jaeger ◄─ OTLP spans (api, worker, verifier)   cadvisor (containers)  │
                         └──────────────────────────────────────────────────────────────────────────┘
```

| Boundary | Why it is separate |
|---|---|
| **api** | I/O-bound and latency-sensitive (validation, enqueue, polling). It scales by connections, not by model calls. It can be released on its own (contract changes) without touching agents. If workers crash, submissions are still accepted and queued. |
| **agent-worker** | Holds the embedding model and FAISS index (~400 MB RSS) and spends most of its time waiting on the model. Scales horizontally (`--scale agent-worker=N`), and each worker runs `WORKER_CONCURRENCY` runs at once. A crash loses at most its in-flight runs. The API reports those as `failed` after `RUN_DEADLINE_S`, never as partial answers. |
| **verifier** | A second agent with a different responsibility and its own release cadence (verification policy). A failure here cannot let an unverified answer out: the worker escalates (`status=degraded`) if the verifier is unreachable. |
| **llm-stub** | Stands in for the provider. Load tests then measure our system, not the provider's rate limits (D6). Switching to a real provider is configuration only. |
| **indexer** | Batch job with a different resource profile (CPU-heavy embedding). It runs once at startup, is idempotent, and on rerun re-embeds only changed chunks. |
| **redis** | Queue, run state, caches and rate limits in one small stateful dependency. Persistence is AOF. Runs are also written to `/data/runs/<id>.json`. |
| prometheus / grafana / jaeger / cadvisor | Observability is isolated from the request path. If it is down, the service still serves. |

### Why asynchronous (A4)

A run makes about 10 sequential model calls. That is about 13 s with the stub and tens of seconds with a free-tier provider. A synchronous request would hold an API worker for the whole run and hit proxy idle timeouts. It would also let one client's burst block everyone else. `POST` therefore returns `202 {run_id}` at once and the client polls `GET /v1/runs/{id}` (or passes `callback_url`). Polling was chosen as the primary mechanism because the web client is a browser that cannot receive callbacks. Callbacks are still supported for server-to-server clients. The Redis queue between submission and execution absorbs bursts, makes back-pressure visible (`run_queue_depth`) and lets workers scale independently.

## Agent view (A1–A3)

```
 input_guard ──blocked──────────────────────────────────────────────┐
     │ ok                                                           │
 intake_agent  (LLM + lexical priors: product → entity → regulator) │
     │  hand-off: IntakeReport {product, entity_type, domain,       │
     │            candidate_regulators, split, out_of_corpus}       │
     ▼                                                              │
 research_agent ◄───────┐  one tool call per step, chosen by the    │
     │  finish          │  model: search_corpus | get_clause |      │
     ▼                  │  get_complaint_field | list_regulator_scope
 draft_determination    │                                           │
     │ hand-off: {determination, evidence, facts} over HTTP         │
     ▼                  │ verifier hints (missing scope/timeline,   │
 verify (verifier agent)┘ unresolved clause) → re-search with a     │
     │ verified / attempts exhausted     different strategy         │
     ▼                                                              │
 finalize: F7 escalation rules, degraded handling, output filter ◄──┘
```

**Agents and responsibilities.**

| Agent | Where | Responsibility | Prompt |
|---|---|---|---|
| Intake agent | worker | Classifies the domain (F3). Reasons product → entity → regulator and ignores the regulator the complainant names. Reconciles the model's answer with lexical priors. | `intake@v1/v2` |
| Research agent | worker | Chooses the tools. Retrieves what appears to apply, then looks for modifiers (the other regulator's scope, exclusions, maintainability), then re-queries from what it has read. Drafts the determination. | `planner@v1`, `determine@v1/v2` |
| Verifier agent | **verifier service** | Deterministic checks: each citation resolves to a retrieved passage, each quote is verbatim, the route has the provisions it depends on, and the basis is grounded. Then an LLM entailment judgement. Returns failures and hints. | `verify_judge@v1` |
| Router | worker (`finalize`) | Deterministic policy. Escalates on split, out-of-corpus, unverified, degraded or loop-bound. Applies the output filter. | none |

**Loop bounds (A1):** `AGENT_MAX_STEPS=30` graph steps, `AGENT_MAX_TOOL_CALLS=10` (+3 per verification retry), `AGENT_MAX_WALL_CLOCK_S=90`, `AGENT_MAX_VERIFY_ATTEMPTS=3`, `TOOL_TIMEOUT_S=5`, plus LangGraph's `recursion_limit` as a hard backstop.

**Steps 2 and 4 of section 5.** *Step 2:* after the first search, the planner must check the scope of every candidate regulator (`list_regulator_scope`) and the exclusions of whichever regulator the first search pointed at. For the 4.1 case it finds SCORES-2 ("complaints against entities that are not regulated by SEBI … are not dealt with") and RBIOS-2/3A/8. *Step 4:* the verifier rejects drafts whose citations do not resolve or whose route lacks its required provisions. Its hints (`missing_scope:RBI`, `missing_type:timeline@SEBI`, `lookup_clause:X`) become targeted re-searches. If the draft still fails after 3 attempts, the run is escalated.

### What happens when something fails mid-run

| Failure | Behaviour | Status returned |
|---|---|---|
| Model provider errors or times out | Bounded retries with exponential backoff and jitter. After 3 consecutive failures the breaker opens and later calls fail fast. The run falls back to the rule-based path, its route is marked *provisional*, and it is escalated. | `degraded` |
| Verifier service down or slow | Timeout (`VERIFIER_TIMEOUT_S`). The draft is never returned as an answer. | `degraded` (escalated) |
| A graph node raises | Caught by the node wrapper and escalated with the error class. | `failed` |
| Worker process dies mid-run | The job is lost with the process. `GET /v1/runs/{id}` reports `failed` after `RUN_DEADLINE_S` (lazy reaper). The client may resubmit. | `failed` |
| Redis down | API `/health` returns 503 and submissions fail with 5xx. Workers retry their loop every second. | n/a |
| Tool error or timeout | Recorded in the step and counted against the tool budget. The agent continues with other tools. | normal |

## Retrieval design (L1)

* **Chunking:** one chunk per clause. Clauses longer than 1,200 characters are split at sentence boundaries with 200 characters of overlap. Each chunk is embedded with a context header `[Regulator: RBI | Reserve Bank - Integrated Ombudsman Scheme, 2021 | Clause RBIOS-8: Grounds of complaint | type: grounds]`.
* **Metadata:** `regulator`, `clause_type` (scope, exclusion, definition, grounds, maintainability, timeline, responsibility, obligation, procedure) and `entities`. The agent uses filtered searches (`regulator=RBI, clause_type=maintainability`).
* **Why this addresses section 3:** a complaint about a bank product written in investment vocabulary embeds close to SEBI text. Measured: naive top-4 for the 4.1 complaint returned SCORES-9B, MFR-53C, SCORES-9D and SCORES-9E, all SEBI. Similarity search will not fix this alone. The fix is to search **by framework**: the intake agent infers the entity (bank), and the research agent queries the RBI framework explicitly and checks SEBI's exclusions. The regulator header and metadata make those scoped queries possible and precise.
* **Embeddings:** `EMBEDDING_MODEL` (all-MiniLM-L6-v2 in `.env.example`), run locally, normalised, inner-product FAISS index through LangChain.
* **Re-embedding without a full rebuild:** chunk IDs are stable (`doc:clause:n`) and each chunk carries a content hash. A rerun deletes changed or removed IDs from the FAISS index and adds only new vectors. Vectors are cached by `sha256(model+text)`, so even `--full` rebuilds reuse them. Each build writes a manifest: `built_at`, document versions and hashes, `n_chunks`, `n_embedded_this_build`, throughput. These are exposed as metrics (`index_built_timestamp_seconds`, `index_chunks`, `ingest_throughput_chunks_per_second`).

## Observability (D5, A6, F8)

* **Logs:** JSON (structlog) everywhere, each line with `service`, `correlation_id` and `run_id`.
* **Correlation ID:** taken from `X-Correlation-ID` (or generated), returned in the response, carried in the queue job, sent to the verifier and the model provider as a header, and recorded on every step.
* **Traces:** OpenTelemetry with OTLP export to Jaeger. The trace context is injected into the queue job so the worker's `agent_run` span is a child of the API request. There are spans per graph node, per tool call and per model call.
* **Metrics:** Prometheus scrapes api, every worker replica (DNS service discovery), verifier, llm-stub and cadvisor. The Grafana dashboard `ops/grafana/dashboards/grievance.json` is generated by `scripts/make_dashboard.py`.
* **Run reconstruction:** `GET /v1/runs/{id}/trace` returns every step: tool, args/query, passages with scores, prompt `id@version#sha`, per-step latency, tokens and cost, and verifier failures and hints. It is backed by Redis and by `/data/runs/<id>.json`.

## Guardrails (L4)

1. **Detection:** regex and heuristic injection scoring on every free-text field (`complaint_text`, `entity_name`, `product`). A strong hit blocks the run before any model that holds tools is called.
2. **Isolation:** free text is sanitised (delimiter look-alikes removed, `<<`/`>>` neutralised, abuse masked). It reaches models only inside a JSON `DATA` block that the system prompt declares as untrusted.
3. **Least privilege:** all four tools are read-only. No tool can change a route or contact anyone.
4. **Output filter:** routes come from an allow-list and the routed regulator must be in the corpus. Language that disposes of a complaint, awards or penalises is blocked. Toxicity and injection checks also run on the output.
5. **Toxicity:** a lexicon and threat patterns on input (flag, mask, block on threats) and on output.
