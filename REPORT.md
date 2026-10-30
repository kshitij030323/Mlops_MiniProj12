# Report: Securities Investor Grievance Routing Service

**Mini Project 12 · UE24AM342AA5 · Team:** Kshitij Gupta (PES1UG24AM915), Vinay Rampur (PES1UG24AM455), Vikas R (PES1UG24AM477)

## 1. Problem and scope

Retail investors write to SEBI about brokers, mutual funds, registrars, listed companies and depositories, and sometimes about things SEBI does not regulate. The service makes the first two decisions on a complaint:

* **Jurisdiction:** does SEBI deal with it at all?
* **Responsibility:** which regulated entity owns it, and therefore which redressal timeline applies.

It returns a **recommendation** with circular-level citations and never disposes of a complaint. It escalates when the complaint splits across regulators, names an entity type outside the corpus, or when it cannot verify its own answer.

In scope: a REST service with published contracts, ingestion validation and quarantine, a LangGraph multi-agent reasoning layer with self-verification, a corpus of three frameworks (SEBI SCORES master circular, SEBI MF Regulations servicing provisions, RBI Integrated Ombudsman Scheme 2021), an evaluation harness, guardrails, observability, Docker Compose deployment, and a load test against a declared SLO.

Out of scope: model training, cloud deployment, real complaints or portals (all scenarios are synthetic).

## 2. Agent design

**Topology** (details and diagrams in `docs/ARCHITECTURE.md`): `input_guard → intake agent → research agent (tool loop) → draft → verifier agent (separate service) → finalize`, with a back-edge from verify to research.

| | |
|---|---|
| Framework | LangGraph `StateGraph`. Retrieval is a **tool the model chooses** (the planner returns `{action, args}`), not a fixed stage. |
| Tools (4, read-only) | `search_corpus(query, regulator?, clause_type?)`, `get_clause(clause_id)`, `get_complaint_field(field)`, `list_regulator_scope(regulator)` |
| Agents | **Intake** (worker process): product → entity → regulator. **Research** (worker process): plans, retrieves, drafts. **Verifier** (own container/service): deterministic citation and grounding checks plus an LLM entailment judge. Hand-offs are typed payloads: IntakeReport inside the graph state, and `{determination, evidence, facts}` over HTTP to the verifier. |
| Verification (A3) | Every citation must resolve to a retrieved passage and quote it verbatim. The route must carry the provisions it depends on: scope of the target regulator for a referral, responsibility + timeline for an assignment, a maintainability clause for not-maintainable. The basis must be grounded (≥ 0.6 token support). On failure the verifier returns hints (`missing_scope:RBI`, `lookup_clause:X`, `missing_type:timeline@SEBI`) that become targeted re-searches, which is a different strategy from the first, similarity-led pass. After 3 attempts the run is escalated. An unverified answer is never returned. |
| Loop limits | 30 graph steps, 10 tool calls (+3 per retry), 90 s wall clock, 3 verification attempts, 5 s per tool, 20 s per model call, LangGraph recursion limit as backstop. |
| Failure mid-run | Provider down: retries, then breaker, then rule-based path marked **degraded**, which escalates. Verifier down: **degraded**, escalated. Node exception: **failed**, escalated. Worker death: API reports **failed** after the deadline. See the table in `docs/ARCHITECTURE.md`. |

**How the 4.1 case is solved (section 5, steps 1–5).** The intake agent establishes *fixed deposit → bank → RBI* and ignores "I want to complain to SEBI" and "invested". The research agent:

1. runs the plain search first (it returns SEBI passages only: SCORES-9B, MFR-53C, SCORES-9D, SCORES-9E);
2. asks what modifies them: `list_regulator_scope(RBI)` and SEBI's exclusions (finds **SCORES-2**);
3. re-searches with derived queries ("customer aggrieved by act or omission of Regulated Entity …" @RBI, "written complaint to the Regulated Entity … 30 days" @RBI/maintainability).

It drafts `refer_to_other_regulator → RBI` citing RBIOS-2, RBIOS-3A, RBIOS-8, RBIOS-10B and SCORES-2. The verifier resolves all five citations. Result over the API: `completed`, 13.3 s, 10 model calls, $0.008.

## 3. Architecture and deployment boundaries

Ten containers: `api`, `agent-worker` (scalable), `verifier`, `llm-stub`, `indexer` (one-shot), `redis`, `prometheus`, `grafana`, `jaeger`, `cadvisor`, plus `locust` under a profile. The diagram with boundaries and the justification for each (scaling, failure isolation, independent release, resource profile) is in `docs/ARCHITECTURE.md`. In short:

* the API is I/O-bound and scales by connections;
* the worker is memory-heavy (model + index) and scales by model-wait concurrency;
* the verifier is isolated so that its failure fails safe;
* the stub swaps for a provider by configuration;
* the indexer is a batch job with a CPU profile.

**Async execution (A4):** `POST` returns 202 + `run_id`, with a Redis queue in between, then polling (browser-friendly) or an optional `callback_url`. A run takes tens of seconds, which is too long to hold an HTTP request.

## 4. Design decisions and what was rejected

| Decision | Rejected alternative | Why |
|---|---|---|
| Jurisdiction-first: infer the entity, then search **by framework** with metadata filters | Better embeddings or a re-ranker on one similarity search | The 4.1 complaint is semantically closest to SEBI text. No ranking fixes a passage that is never retrieved. The RBI provisions never mention "fixed deposit". |
| Clause-level chunks with a regulator/clause context header | Fixed 500-char windows | Measured: windows lose clause attribution and the regulator signal (section 5). |
| Verifier as a **separate service** | A second prompt in the worker | The brief rules out two prompts in one process. It also isolates failure: if verification is unavailable, nothing is issued. |
| Deterministic verification first, then LLM judge | LLM-only judge | Citation resolution is exactly checkable. An LLM judge alone can be talked round, and its noise would confound before/after metrics. |
| Lexical priors reconciled with the model's intake | Trust the model's classification | Models are most often misled by investment vocabulary (section 3). When the rules have strong product evidence that contradicts the model, the rule reading wins and the disagreement is recorded. |
| Redis list as queue | Celery / RabbitMQ | One fewer moving part. Redis is already needed for state, caches and rate limits. At-most-once delivery is acceptable because lost runs surface as `failed` and are idempotently resubmittable. |
| OpenAI-compatible HTTP client behind our own interface | Provider SDKs | Provider changes are configuration only. The same protocol serves the stub. |
| Polling as primary async mechanism | Callbacks only / WebSockets | The web client is a browser. Callbacks are kept for server clients. |
| Three caches: query-embedding LRU, retrieval (Redis, keyed by index version), whole-run (Redis, keyed by normalised complaint + prompt versions + index version) | Caching LLM responses by prompt | Whole-run caching removes all model calls for a duplicate complaint (common: investors resubmit). Keying on prompt and index versions prevents stale answers after a change. |

## 5. Evaluation (L2, L3)

24 fixed scenarios in `eval/scenarios.jsonl`, each with the expected outcome and the supporting passages. They cover the 4.1 case, all five SEBI domains, RBI products (UPI, ATM, credit card, NBFC, RD), out-of-corpus (insurance, NPS, crypto), a split case, time-barred, vague and prompt-injection cases. Run from the CLI: `python -m eval.run_eval`. Metric definitions are in the harness docstring and in `docs/EVALUATION.md`.

Results, the deliberate change (fixed-window → clause-aware chunking) with all four metrics before and after, and the prompt-variant comparison are in **`docs/EVALUATION.md`**.

> Important caveat. All numbers in this repository were produced with the **LLM stub**, whose answers come from deterministic rules (`grievance/rules.py`). They therefore measure retrieval, verification and orchestration, not a language model's judgement. The prompt-variant comparison under the stub can only show token and cost differences. Re-run `eval.run_eval --variant v1/v2` with a real provider configured to compare quality.

## 6. SLO, load test, bottleneck, cost

* **SLO** (declared before measuring, `docs/SLO.md`): p95 end-to-end run ≤ 30 s at ≥ 0.5 runs/s.
* **Measured under Docker Compose** (stub at 1.2 s/call, `docs/LOADTEST.md`). 1 worker at 16 users: 0.31 runs/s, p95 50 s ❌. 3 workers: 0.87 runs/s, p95 21 s ✅. Earlier local-process runs:
  * 1 worker plateaus at **0.28 runs/s**. p95 is 15 s at 4 users and 29 s at 8, then **55 s at 16 users, where the target fails**.
  * With `--scale agent-worker=3` at 16 users: **0.88 runs/s, p95 20 s, so the target is met**.
  * Bottleneck: worker run slots held while waiting on the model's 10 sequential calls (4 slots / 13.5 s ≈ 0.30 runs/s per worker). It is not the API (submit p95 < 30 ms), Redis or CPU.
  * The run cache cuts a repeated complaint to about 10 ms and $0.
* **Evaluation headline** (stub): 24/24 routes correct, groundedness 0.68, evidence recall 1.00, faithfulness 0.95, hallucination 0.00. The deliberate change (fixed → clause chunking) moved route accuracy from 0.83 to 1.00 and groundedness from 0.59 to 0.68.
* **Cost:** about **$0.73 per 100 runs** at the example prices. The largest driver is the research agent's planner loop (≈ 74 % of tokens), see `docs/COST.md`.

## 7. What did not work / limitations

* **Corpus wording is condensed.** `corpus/raw/*.txt` are clause-level extracts written for indexing. The passages the 4.1 case depends on must be checked against the official PDFs at Evaluation 1. The pipeline indexes official PDFs dropped into `corpus/raw/` (text layer required), but PDF clause typing falls back to `other`, which weakens filtered search.
* **Stub-only evaluation.** No real-provider numbers were produced in development (no key was available in the build environment). The planner prompt has not been tuned against a real model's tool-choice errors. The rule fallback in `research_agent` covers malformed actions.
* **Groundedness is lexical.** Token overlap catches fabricated content but not a well-worded misreading. The LLM judge in the verifier is the second line, and it is noisy.
* **Lexical toxicity and injection detection** catches common patterns but not paraphrased attacks. That is why the defence is layered: isolation, read-only tools, and an output filter.
* **The circuit breaker is per process**, so each worker learns of an outage separately (three wasted calls per worker). A shared breaker in Redis was considered but not built.
* **At-most-once queue.** A worker crash loses in-flight runs (reported as `failed` after the deadline) rather than retrying them.
* **Slow builds on slow networks.** The image pulls ~1 GB of Python wheels (torch, transformers). On the development network, pip inside the Docker VM timed out twice. The fix (optional host-side wheelhouse + pip cache mount, see README) works, but it is a workaround for the environment, not something the system needs.
