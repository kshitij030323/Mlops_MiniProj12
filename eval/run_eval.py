"""Batch evaluation from the command line (L2, L3).

    python -m eval.run_eval                               # default prompt variant, configured index
    python -m eval.run_eval --variant v1 --tag v1         # compare prompt variants on the same set
    python -m eval.run_eval --index-dir /data/index_fixed --tag fixed_chunks   # the deliberate change
    python -m eval.run_eval --naive                       # also show single-pass retrieval for each scenario

Runs the same LangGraph agents as the worker, in-process: the model is whatever LLM_BASE_URL points at (the
stub by default), the verifier agent logic runs in-process, and there is no Redis. Writes eval/results/<tag>.json.

Metric definitions (computed per scenario, then averaged; TruLens-equivalent, deterministic so before/after
comparisons are not confounded by judge-model noise):
  groundedness       mean over the claims (sentences) in the determination basis of the fraction of the claim's
                     content tokens found in the *cited* passages or in the complaint's own fields.
  context_relevance  of the passages retrieved during the run, the fraction that belong to the scenario's
                     relevant-clause set (precision). evidence_recall = fraction of supporting clauses retrieved.
  faithfulness       fraction of citations whose quote occurs verbatim (normalised) in a retrieved passage AND
                     whose regulator matches the routed regulator (or is another regulator's exclusion clause, e.g.
                     SCORES-2 "SEBI does not deal with entities it does not regulate" in an RBI referral).
  hallucination_rate over runs that returned a non-escalated answer: share with any citation that does not resolve
                     to retrieved text, or groundedness < 0.6, or a regulator/route contradicting the expected one.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_scenarios(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            s = json.loads(line)
            s["request"]["incident_date"] = (dt.date.today() - dt.timedelta(days=s["incident_days_ago"])).isoformat()
            out.append(s)
    return out


def score(s: dict, state: dict) -> dict:
    from grievance.verification import norm, verify

    final = state["final"]
    det = final["determination"] or {}
    exp = s["expected"]
    ev = state.get("evidence") or []
    retrieved = {e["clause_id"] for e in ev}
    escalated = final["escalation"]["required"]
    route_ok = det.get("route") == exp["route"]
    reg_ok = exp["escalate"] or det.get("regulator") == exp["regulator"]
    # use the draft (pre-escalation) for citation metrics so escalated runs are also measured
    draft = state.get("draft") or det
    from grievance.agents.graph import _facts
    v = verify(draft, ev, _facts(state)) if draft else {"scores": {"groundedness": 0.0}}
    cites = draft.get("citations") or []
    by_pid = {e["passage_id"]: e for e in ev}
    by_cid = {e["clause_id"]: e for e in ev}

    def _faithful(c):
        e = by_pid.get(c.get("passage_id")) or by_cid.get(c.get("clause_id"))
        return bool(e) and norm(c.get("quote", "")) in norm(e["text"]) and (
            e["regulator"] == draft.get("regulator") or e["clause_type"] == "exclusion")
    faithful = [c for c in cites if _faithful(c)]
    relevant = set(s["relevant_clauses"])
    ctx_rel = (sum(e["clause_id"] in relevant for e in ev) / len(ev)) if ev and relevant else None
    recall = (len(set(s["supporting_clauses"]) & retrieved) / len(s["supporting_clauses"])) if s["supporting_clauses"] else None
    returned = not escalated
    halluc = None
    if returned:
        halluc = int(len(faithful) < len(cites) or v["scores"]["groundedness"] < 0.6 or not (route_ok and reg_ok))
    return {"id": s["id"], "expected_route": exp["route"], "route": det.get("route"),
            "provisional_route": det.get("provisional_route"), "regulator": det.get("regulator"),
            "status": final["status"], "route_correct": route_ok, "regulator_correct": reg_ok,
            "escalation_correct": escalated == exp["escalate"], "groundedness": v["scores"]["groundedness"],
            "faithfulness": (len(faithful) / len(cites)) if cites else None, "context_relevance": ctx_rel,
            "evidence_recall": recall, "hallucinated": halluc, "citations": [c.get("clause_id") for c in cites],
            "retrieved": sorted(retrieved), "steps": state.get("steps"), "tool_calls": state.get("tool_calls"),
            "verify_attempts": state.get("attempt"), "usage": state.get("usage"), "prompt_refs": state.get("prompt_refs")}


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 4) if xs else None


def summarise(rows: list[dict]) -> dict:
    returned = [r for r in rows if r["hallucinated"] is not None]
    usage = [r["usage"] or {} for r in rows]
    return {"n": len(rows),
            "route_accuracy": mean([float(r["route_correct"]) for r in rows]),
            "regulator_accuracy": mean([float(r["regulator_correct"]) for r in rows]),
            "escalation_accuracy": mean([float(r["escalation_correct"]) for r in rows]),
            "groundedness": mean([r["groundedness"] for r in rows]),
            "context_relevance": mean([r["context_relevance"] for r in rows]),
            "evidence_recall": mean([r["evidence_recall"] for r in rows]),
            "faithfulness": mean([r["faithfulness"] for r in rows]),
            "hallucination_rate": mean([float(r["hallucinated"]) for r in returned]),
            "mean_tool_calls": mean([r["tool_calls"] for r in rows]),
            "mean_prompt_tokens": mean([u.get("prompt_tokens", 0) for u in usage]),
            "mean_completion_tokens": mean([u.get("completion_tokens", 0) for u in usage]),
            "mean_cost_usd": mean([u.get("cost_usd", 0) for u in usage]),
            "mean_llm_calls": mean([u.get("llm_calls", 0) for u in usage])}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default=str(ROOT / "scenarios.jsonl"))
    ap.add_argument("--variant", default=os.getenv("PROMPT_VARIANT", "v2"))
    ap.add_argument("--index-dir", default=None)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--only", default=None, help="comma-separated scenario ids")
    ap.add_argument("--naive", action="store_true", help="also report single-pass top-k retrieval")
    a = ap.parse_args()
    if a.index_dir:
        os.environ["INDEX_DIR"] = a.index_dir
    from grievance.agents.graph import RunContext, run_graph
    from grievance.agents.verifier_agent import run_verifier
    from grievance.retrieval import retriever

    rt = retriever()
    scenarios = load_scenarios(Path(a.scenarios))
    if a.only:
        keep = set(a.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in keep]
    rows, traces = [], {}
    for s in scenarios:
        steps: list[dict] = []

        async def record(step, steps=steps):
            steps.append(step)

        ctx = RunContext(rt, record, lambda p, cid: run_verifier(p, cid))
        t0 = time.perf_counter()
        state = await run_graph(ctx, s["id"], f"eval-{s['id']}", s["request"], a.variant)
        row = score(s, state)
        row["latency_s"] = round(time.perf_counter() - t0, 3)
        if a.naive:
            row["naive_top_k"] = [f"{p['clause_id']}({p['regulator']})" for p in rt.search(s["request"]["complaint_text"], 4)]
        rows.append(row)
        traces[s["id"]] = steps
        ok = "OK " if row["route_correct"] and row["regulator_correct"] else "XX "
        print(f"{ok}{s['id']:<32} expected={s['expected']['route']:<26} got={row['route']:<26} "
              f"reg={row['regulator']} grounded={row['groundedness']:.2f} recall={row['evidence_recall']}"
              + (f" naive={row['naive_top_k']}" if a.naive else ""))
    summary = summarise(rows)
    summary.update(variant=a.variant, index_version=rt.version, chunking=rt.manifest["chunking"],
                   llm_base_url_kind="stub" if "stub" in os.getenv("LLM_BASE_URL", "") else "configured",
                   generated_at=dt.datetime.now().isoformat(timespec="seconds"))
    tag = a.tag or f"{a.variant}_{rt.manifest['chunking']}"
    out = ROOT / "results" / f"{tag}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "rows": rows, "traces": traces}, indent=1, default=str))
    print(json.dumps(summary, indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
