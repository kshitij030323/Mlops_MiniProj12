"""Verifier agent (A2, A3). Runs in its own service; the worker hands off a draft over HTTP.

Two layers: deterministic checks (citations resolve to retrieved text, required provisions present, basis grounded),
then an LLM entailment judgement. If the LLM is unavailable the deterministic verdict stands but is reported as
`llm_judge: skipped`, and the worker treats the run as degraded (escalated), never as a confident answer.
"""
from __future__ import annotations

from .. import llm, prompts
from ..verification import verify


async def run_verifier(payload: dict, correlation_id: str = "-") -> dict:
    det, evidence = payload["determination"], payload["evidence"]
    result = verify(det, evidence, payload.get("facts"))
    result["llm_judge"] = "not_needed"
    result["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
    if result["verified"] and det.get("route") != "escalate_to_human":
        cited = {c.get("passage_id") for c in det.get("citations", [])} | {c.get("clause_id") for c in det.get("citations", [])}
        data = {"determination": {k: det.get(k) for k in ("route", "regulator", "responsible_entity", "timeline",
                                                          "reason", "basis")},
                "cited_passages": [{"clause_id": e["clause_id"], "text": e["text"]} for e in evidence
                                   if e["passage_id"] in cited or e["clause_id"] in cited],
                "complaint_facts": [f for f in payload.get("facts") or [] if f]}
        msgs, ref = prompts.render("verify_judge", "v1", data)
        result["prompt_ref"] = ref
        try:
            out = await llm.chat(msgs, agent="verifier", step="verify_judge", correlation_id=correlation_id,
                                 max_tokens=300)
            judged = llm.parse_json(out.content)
            result["usage"] = {"prompt_tokens": out.prompt_tokens, "completion_tokens": out.completion_tokens,
                               "cost_usd": out.cost_usd}
            result["llm_judge"] = "supported" if judged.get("supported") else "unsupported"
            if not judged.get("supported"):
                result["verified"] = False
                result["failures"].append({"code": "llm_judge_unsupported",
                                           "detail": judged.get("unsupported_claims", [])[:3]})
        except (llm.LLMUnavailable, ValueError) as e:
            result["llm_judge"] = "skipped"
            result["llm_judge_error"] = str(e)[:200]
    return result
