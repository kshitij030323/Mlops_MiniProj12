# Evaluation (L2, L3)

* **Set:** `eval/scenarios.jsonl`. 24 synthetic scenarios, each with `expected` (route, regulator, domain, escalate), the `supporting_clauses` the outcome rests on, and a wider `relevant_clauses` set. Incident dates are relative (`incident_days_ago`), so the time-bar case stays valid over time.
* **Run:** `python -m eval.run_eval [--variant v1|v2] [--index-dir DIR] [--tag NAME]`. It runs the same LangGraph agents as the worker, in-process (verifier logic in-process, no Redis), and writes `eval/results/<tag>.json` with per-scenario rows and full step traces.
* **Model:** all numbers below use the **LLM stub** (deterministic rule responses, see `REPORT.md` §5). They measure retrieval, verification and orchestration.

## Metrics: how each is computed (TruLens-equivalent, deterministic)

| Metric | Computation |
|---|---|
| Groundedness | For each claim (sentence) in the determination's `basis`: the fraction of its content tokens (stop-words removed) found in the **cited** passages or in the complaint's own fields. Averaged over claims, then over scenarios. Same function the verifier enforces (≥ 0.6). |
| Context relevance | Of all passages retrieved during the run, the fraction in the scenario's `relevant_clauses` (precision). Reported with **evidence recall**: the fraction of `supporting_clauses` retrieved. |
| Faithfulness | The fraction of citations whose quote occurs verbatim (normalised) in the retrieved passage it names, **and** whose regulator is the routed regulator (or which is another regulator's *exclusion* clause, e.g. SCORES-2 in an RBI referral). |
| Hallucination rate | Over runs that returned a non-escalated answer: the share with any citation that does not resolve to retrieved text, groundedness < 0.6, or a route or regulator that contradicts the expected one. |
| Route / regulator / escalation accuracy | Exact match with `expected`. |

## Reference case (section 4.1): the values we test with

Request: `complaint_text` = "I invested in a fixed deposit through my bank's app. The interest credited is lower than what I was promised. I want to complain to SEBI.", `entity_name` = "Example Bank Ltd", `regulator_addressed` = "SEBI", `incident_date` = 20 days ago, `amount_inr` = 200000.

Expected: `route = refer_to_other_regulator`, `regulator = RBI`, `domain = out_of_jurisdiction`, `entity_type = bank`. Citations: **RBIOS-2** (Scheme applies to Regulated Entities, incl. commercial banks), **RBIOS-3A** (Regulated Entity means a bank …), **RBIOS-8** (grounds: deficiency in service), **RBIOS-10B** (complain in writing to the bank first, then wait 30 days), **SCORES-2** (SEBI does not deal with entities it does not regulate). Timeline: first a written complaint to the bank, then the Ombudsman if there is no reply in 30 days or the reply is unsatisfactory, within one year.

Single-pass retrieval for this complaint (top-4, measured): **SCORES-9B, MFR-53C, SCORES-9D, SCORES-9E, all SEBI**. This is why a naive RAG pipeline fails here. The agent gets the right answer through `list_regulator_scope(RBI)`, the SEBI exclusion search and RBI-scoped re-queries (trace in `eval/results/clause_v2.json → traces.S01_fd_interest_hard_case`).

## Deliberate change: fixed-window chunking → clause-aware chunking with context header

*Before:* `--chunking fixed`: 500-char windows, 100 overlap, no regulator/clause header, clause metadata only for the clause the window starts in. *After:* `--chunking clause` (production): one chunk per clause, with a `[Regulator | Document | Clause | type]` header. Same 24 scenarios, prompt v2, same stub.

| Metric | Before (fixed) | After (clause) | Δ |
|---|---|---|---|
| **Groundedness** | 0.590 | **0.677** | +0.087 |
| **Context relevance** (precision) | 0.429 | 0.401 | −0.028 |
| Evidence recall | 0.939 | **1.000** | +0.061 |
| **Faithfulness** | 0.955 | 0.955 | 0 |
| **Hallucination rate** (returned answers) | 0.000 | 0.000 | 0 |
| Route accuracy | 0.833 | **1.000** | +0.167 |
| Escalation accuracy | 0.833 | **1.000** | +0.167 |
| Mean prompt tokens / run | 13,596 | **11,401** | −16 % |
| Cost / run (USD) | 0.0087 | **0.0073** | −16 % |

Reading:

* With fixed windows, four RBI cases (UPI, credit card, NBFC, ATM) had the right *provisional* route but were escalated: windows cut provisions mid-sentence, so the basis could not be grounded above 0.6. The system failed safe (hallucination stays 0), but at the cost of 17 % unnecessary escalations.
* Evidence recall fell for the MF cases (S03: 0.33) because MFR-53A/B lost their clause identity inside shared windows.
* Context relevance is slightly *higher* with windows only because each window spans several relevant clauses. That is a precision artefact, not better retrieval.
* Prompt tokens rise 16 % with fixed windows because the agent retries.

## Prompt variants (L2): determine/intake v1 vs v2

| Metric | v1 | v2 |
|---|---|---|
| Route accuracy | 1.000 | 1.000 |
| Groundedness / faithfulness / hallucination | 0.677 / 0.955 / 0.000 | 0.677 / 0.955 / 0.000 |
| Mean prompt tokens / run | **11,009** | 11,401 (+3.6 %) |
| Cost / run | **$0.0071** | $0.0073 |

Under the stub, quality is identical **by construction**: the stub's answers do not depend on prompt wording. So the only valid conclusion is that **v1 is 3.6 % cheaper in prompt tokens**. v2 (jurisdiction-first reasoning, verbatim-quote rule, explicit escalation conditions) is the production default because it targets the failure section 3 describes. Its quality advantage must be measured with a real provider:

```bash
# .env: LLM_BASE_URL / LLM_MODEL / LLM_API_KEY of a free-tier provider
docker compose exec agent-worker python -m eval.run_eval --variant v1 --tag real_v1
docker compose exec agent-worker python -m eval.run_eval --variant v2 --tag real_v2
```

Every response records the prompt versions used (`prompt_versions`: `intake@v2#<sha>`, …).

## Live quality (L6)

The worker scores a sample (`LIVE_EVAL_SAMPLE_RATE`, default 20 %) of live runs with the reference-free versions of the metrics: groundedness and citation resolution from the verifier, context relevance as the share of retrieved passages from the routed/candidate regulator, and hallucination = returned but unverified. These go to Prometheus (`live_eval_score`, `live_eval_last_score`, charted over time in Grafana) and to `GET /v1/quality`. Caller feedback (`POST /v1/runs/{id}/feedback`) is persisted (Redis + `/data/feedback/feedback.jsonl`) and aggregated at `GET /v1/feedback/summary`.
