# Cost (L5)

Tokens and cost are recorded **at call time** for every model call (`grievance/llm.py`). Prices are taken from `PRICE_PROMPT_PER_1K_USD` / `PRICE_COMPLETION_PER_1K_USD`, which are injected configuration, so the figure follows the provider in use. Cost is attributed:

* **per step:** each step in `GET /v1/runs/{id}/trace` carries `prompt_tokens`, `completion_tokens`, `cost_usd`; Prometheus `llm_cost_usd_total{agent,step}` and `llm_tokens_total{agent,step,kind}`;
* **per run:** `usage` in the run result (`prompt_tokens`, `completion_tokens`, `cost_usd`, `llm_calls`).

## Measured (eval set, 24 scenarios, clause index, prompt v2)

Token counts come from the stub, which reports `len(prompt)/4` and `len(completion)/4`, so they follow the real prompt sizes. Prices are the example values in `.env.example` ($0.59 / $0.79 per 1M tokens, typical of a small hosted open-weight model). Change the prices and the costs scale linearly.

| Step | Calls/run | Prompt tok/run | Completion tok/run | Cost/run (USD) | Share |
|---|---|---|---|---|---|
| research_agent (planner) | 7.25 | 8,409 | 322 | 0.00522 | **73.7 %** |
| draft_determination | 0.96 | 2,149 | 384 | 0.00157 | 22.2 % |
| intake_agent | 0.96 | 406 | 68 | 0.00029 | 4.1 % |
| verify_judge (verifier service) | ≤1 | n/a | n/a | ≈0.0002 | ≈3 % |
| **Total per run** | 9.9 | 11,401 | 782 | **0.0073** | |

**Cost per 100 runs ≈ $0.73** (prompt v2) and $0.71 (prompt v1). With caching, repeated complaints cost $0: a run-cache hit makes no model call.

**Largest cost driver: the research agent's planner loop.** Each of its ~7 calls re-sends the system prompt, the tool list and the growing evidence summary, so prompt tokens grow roughly quadratically with the number of tool calls. Ways to cut it, in order of effect:

1. Cap evidence snippets in the planner state (already 160 chars) and send only passages added since the last call.
2. Lower `AGENT_MAX_TOOL_CALLS`. Most runs finish in 6–8 calls.
3. Run the planner on a cheaper model than the determination step. The interface already allows a per-task model.

Rate limiting per client (`RATE_LIMIT_PER_MIN`, default 30) is the cost control against a looping client. Without it, one client at 10 req/s would spend about $4.40 per minute at these prices.
