"""LangGraph orchestration of the routing agents (A1, A2, A3, A5, F3-F7).

Topology (see docs/ARCHITECTURE.md):

  input_guard -> intake_agent -> research_agent <-+ (loop: one tool call per step, model chooses the tool)
                                    |             |
                                    v             | verifier hints (retry with a different strategy)
                                  draft -> verify (verifier agent, separate service) --+
                                                    |
                                                    v
                                                 finalize (routing, F7 escalation, output filter) -> END
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, StateGraph

from .. import guardrails, llm, prompts, rules
from ..config import settings
from ..observability import (AGENT_STEP_LATENCY, AGENT_TASKS, DEGRADED_RUNS, GUARDRAIL_EVENTS, TOOL_CALLS,
                             VERIFY_OUTCOMES, log, tracer)
from .tools import TOOL_SPECS, run_tool

ESCALATION_PATH = ("Grievance officer queue (manual triage) -> senior officer for multi-regulator or "
                   "out-of-corpus cases. No determination is issued until an officer decides.")


class RunState(TypedDict, total=False):
    run_id: str
    correlation_id: str
    complaint: dict
    variant: str
    demo_fault: str | None
    started: float
    steps: int
    tool_calls: int
    tool_bonus: int
    guard: dict
    intake: dict
    evidence: list
    tool_history: list
    verifier_hints: list
    attempt: int
    draft: dict
    verification: dict
    degraded: bool
    degraded_reason: str
    finished_research: bool
    prompt_refs: dict
    usage: dict
    final: dict
    stop_reason: str


class RunContext:
    """Per-run dependencies, injected so the same graph runs in the worker and in the offline eval harness."""

    def __init__(self, retriever, record: Callable[[dict], Awaitable[None]], verify: Callable[..., Awaitable[dict]],
                 cache_get: Callable[[str], Awaitable[Any]] | None = None,
                 cache_set: Callable[[str, Any], Awaitable[None]] | None = None, use_llm: bool = True):
        s = settings()
        self.retriever, self.record, self.verify = retriever, record, verify
        self.k = s.retrieval_k
        self.use_llm = use_llm
        self._cg, self._cs = cache_get, cache_set

    async def cache_get(self, key):
        return await self._cg(key) if self._cg else None

    async def cache_set(self, key, val):
        if self._cs:
            await self._cs(key, val)


def _ctx(config) -> RunContext:
    return config["configurable"]["ctx"]


def _add_usage(state: RunState, pt: int, ct: int, cost: float) -> dict:
    u = dict(state.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "llm_calls": 0})
    u["prompt_tokens"] += pt
    u["completion_tokens"] += ct
    u["cost_usd"] = round(u["cost_usd"] + cost, 8)
    u["llm_calls"] += 1
    return u


def _over_budget(state: RunState) -> str | None:
    s = settings()
    if time.time() - state["started"] > s.max_wall_clock_s:
        return "wall_clock"
    if state.get("steps", 0) >= s.max_steps:
        return "max_steps"
    return None


async def _llm_json(state: RunState, ctx: RunContext, task: str, version: str, data: dict, agent: str,
                    step: dict, max_tokens: int = 700) -> tuple[dict | None, dict]:
    """Call the model for a JSON answer. Returns (parsed or None, state updates). None => degrade."""
    msgs, ref = prompts.render(task, version, data)
    refs = {**(state.get("prompt_refs") or {}), task: ref}
    step.update(prompt_ref=ref)
    if not ctx.use_llm or state.get("degraded"):
        step.update(llm="skipped (degraded)")
        return None, {"prompt_refs": refs}
    try:
        out = await llm.chat(msgs, agent=agent, step=task, correlation_id=state["correlation_id"],
                             max_tokens=max_tokens)
    except llm.LLMUnavailable as e:
        DEGRADED_RUNS.inc()
        step.update(llm="unavailable", error=str(e)[:200])
        return None, {"prompt_refs": refs, "degraded": True, "degraded_reason": str(e)[:200]}
    step.update(prompt_tokens=out.prompt_tokens, completion_tokens=out.completion_tokens,
                cost_usd=round(out.cost_usd, 8), llm_latency_ms=round(out.latency_s * 1000, 1), llm_retries=out.retries)
    upd = {"prompt_refs": refs, "usage": _add_usage(state, out.prompt_tokens, out.completion_tokens, out.cost_usd)}
    try:
        return llm.parse_json(out.content), upd
    except ValueError as e:
        step.update(parse_error=str(e))
        return None, upd


def node(agent: str, name: str):
    """Wrap a node: span, per-step latency, step counter, step record (D5 reconstruction)."""
    def deco(fn):
        async def wrapped(state: RunState, config) -> dict:
            ctx = _ctx(config)
            step: dict = {"agent": agent, "node": name, "attempt": state.get("attempt", 0)}
            t0 = time.perf_counter()
            with tracer().start_as_current_span(f"{agent}.{name}") as span:
                span.set_attribute("run_id", state["run_id"])
                span.set_attribute("correlation_id", state["correlation_id"])
                try:
                    upd = await fn(state, ctx, step)
                    step.setdefault("outcome", "ok")
                except Exception as e:  # a node failure must never produce a confident answer
                    log.exception("node_failed", node=name)
                    step.update(outcome="error", error=str(e)[:300])
                    upd = {"final": _escalate(state, f"internal error in {name}: {type(e).__name__}", status="failed")}
                dur = time.perf_counter() - t0
                step["latency_ms"] = round(dur * 1000, 1)
                AGENT_STEP_LATENCY.labels(agent, name).observe(dur)
                span.set_attribute("outcome", step["outcome"])
            await ctx.record(step)
            upd["steps"] = state.get("steps", 0) + 1
            return upd
        return wrapped
    return deco


def _escalate(state: RunState, reason: str, status: str = "escalated", provisional: dict | None = None) -> dict:
    det = {"route": "escalate_to_human", "domain": (state.get("intake") or {}).get("domain", "unknown"),
           "regulator": None, "reason": reason, "basis": reason, "citations": []}
    if provisional:
        det = {**provisional, "route": "escalate_to_human", "reason": reason,
               "provisional_route": provisional.get("route")}
    return {"status": status, "determination": det,
            "escalation": {"required": True, "reason": reason, "path": ESCALATION_PATH}}


# ------------------------------------------------------------------ nodes
@node("intake", "input_guard")
async def input_guard(state: RunState, ctx: RunContext, step: dict) -> dict:
    c = state["complaint"]
    res = guardrails.scan_fields({k: c.get(k) for k in ("complaint_text", "entity_name", "product")})
    inj = {k: v.as_dict() for k, v in res.items() if v.injection_hits}
    tox = {k: v.as_dict() for k, v in res.items() if v.toxicity_hits or v.threat}
    for k in inj:
        GUARDRAIL_EVENTS.labels("prompt_injection", "input").inc()
    for k in tox:
        GUARDRAIL_EVENTS.labels("toxicity", "input").inc()
    guard = {"injection": inj, "toxicity": tox,
             "blocked": any(v.injection_blocked for v in res.values()) or any(v.threat for v in res.values())}
    step.update(guard=guard)
    sanitized = {**c, **{k: guardrails.sanitize(c[k]) for k in ("complaint_text", "entity_name", "product") if c.get(k)}}
    upd: dict = {"guard": guard, "complaint": sanitized}
    if guard["blocked"]:
        step["outcome"] = "blocked"
        AGENT_TASKS.labels("intake", "blocked").inc()
        upd["final"] = _escalate(state, "Input guardrail: possible prompt injection or threat detected in a free-text "
                                        "field. No model holding tools was invoked.")
    return upd


@node("intake", "intake_agent")
async def intake_agent(state: RunState, ctx: RunContext, step: dict) -> dict:
    c = state["complaint"]
    prior = rules.classify(c)
    data = {"complaint": {k: c.get(k) for k in ("complaint_text", "entity_name", "entity_type_claimed", "product",
                                                "regulator_addressed")}}
    out, upd = await _llm_json(state, ctx, "intake", state["variant"], data, "intake", step, max_tokens=400)
    if out is None:
        intake, source = prior, "rules"
    else:
        intake, source = {**prior, **{k: out[k] for k in out if k in prior}}, "llm"
        top_prior = max(prior["scores"].values() or [0])
        if out.get("domain") != prior["domain"] and top_prior >= 3 and not prior["split"]:
            # Lexical evidence of the product/entity is strong and contradicts the model: keep the rule reading and
            # record the disagreement. (The model is most often misled by investment vocabulary - section 3.)
            intake, source = {**prior, "model_disagreed": out.get("domain")}, "rules_override"
        intake["candidate_regulators"] = [r for r in dict.fromkeys(
            (intake.get("candidate_regulators") or []) + prior["candidate_regulators"])]
        # Corpus coverage is a fact about our index, not a model judgement: derive it rather than trust the flag.
        intake["out_of_corpus"] = (intake.get("entity_type") in rules.OUT_OF_CORPUS
                                   or any(r not in ("SEBI", "RBI") for r in intake["candidate_regulators"]))
    intake["source"] = source
    step.update(intake={k: intake.get(k) for k in ("product", "entity_type", "domain", "candidate_regulators",
                                                   "split", "out_of_corpus", "source")})
    AGENT_TASKS.labels("intake", "ok").inc()
    return {**upd, "intake": intake}


def _evidence_summary(ev: list[dict]) -> list[dict]:
    return [{"passage_id": e["passage_id"], "clause_id": e["clause_id"], "regulator": e["regulator"],
             "clause_type": e["clause_type"], "title": e.get("title"), "score": e.get("score"),
             "snippet": e["text"][:160]} for e in ev]


@node("research", "research_agent")
async def research_agent(state: RunState, ctx: RunContext, step: dict) -> dict:
    s = settings()
    budget = s.max_tool_calls + state.get("tool_bonus", 0)
    if (why := _over_budget(state)) or state.get("tool_calls", 0) >= budget:
        step.update(action="finish", reason=why or "max_tool_calls", outcome="bounded")
        return {"finished_research": True, "stop_reason": why or "max_tool_calls"}
    pstate = {"complaint": {k: state["complaint"].get(k) for k in ("complaint_text", "entity_name", "product",
                                                                   "incident_date")},
              "intake": {k: state["intake"].get(k) for k in ("product", "entity_type", "entity_name", "domain",
                                                             "candidate_regulators", "split", "out_of_corpus")},
              "evidence_summary": _evidence_summary(state.get("evidence") or []),
              "tool_history": state.get("tool_history") or [],
              "verifier_hints": state.get("verifier_hints") or [],
              "tool_budget_left": budget - state.get("tool_calls", 0),
              "tools": TOOL_SPECS}
    out, upd = await _llm_json(state, ctx, "planner", "v1", pstate, "research", step, max_tokens=250)
    decision = out if out and out.get("action") else rules.plan_next_action(pstate)
    step.update(decision_source="llm" if out and out.get("action") else "rules")
    action, args = decision.get("action"), decision.get("args") or {}
    step.update(action=action, args=args, thought=str(decision.get("thought", ""))[:200])
    if action == "finish" or action not in TOOL_SPECS:
        if action not in ("finish", None) and action not in TOOL_SPECS:
            step["invalid_action"] = action
        return {**upd, "finished_research": True}

    import asyncio
    hist = list(state.get("tool_history") or []) + [{"tool": action, "args": args}]
    t0 = time.perf_counter()
    with tracer().start_as_current_span(f"tool.{action}") as span:
        span.set_attribute("tool.args", str(args)[:300])
        try:
            res = await asyncio.wait_for(run_tool(ctx, state, action, args), timeout=s.tool_timeout_s)
            TOOL_CALLS.labels("research", action, "ok").inc()
        except (asyncio.TimeoutError, ValueError) as e:
            TOOL_CALLS.labels("research", action, "error").inc()
            step.update(tool_error=f"{type(e).__name__}: {e}"[:200])
            return {**upd, "tool_history": hist, "tool_calls": state.get("tool_calls", 0) + 1}
    step["tool_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    ev = list(state.get("evidence") or [])
    have = {e["passage_id"] for e in ev}
    new = [p for p in res.get("passages", []) if p["passage_id"] not in have]
    ev += new
    step.update(query=args.get("query"), cached=res.get("cached"), value=res.get("value"),
                passages=[{"passage_id": p["passage_id"], "clause_id": p["clause_id"], "score": p.get("score"),
                           "text": p["text"][:300]} for p in res.get("passages", [])],
                new_passages=len(new))
    return {**upd, "evidence": ev, "tool_history": hist, "tool_calls": state.get("tool_calls", 0) + 1}


@node("research", "draft_determination")
async def draft(state: RunState, ctx: RunContext, step: dict) -> dict:
    ev = state.get("evidence") or []
    data = {"complaint": {k: state["complaint"].get(k) for k in ("complaint_text", "entity_name", "product",
                                                                 "incident_date", "complained_to_entity_on")},
            "today": dt.date.today().isoformat(),
            "intake": {k: state["intake"].get(k) for k in ("product", "entity_type", "entity_name", "domain",
                                                           "candidate_regulators", "split", "out_of_corpus")},
            "evidence": [{k: e.get(k) for k in ("passage_id", "clause_id", "doc_id", "regulator", "clause_type",
                                                "entities", "reference", "text")} for e in ev],
            "verifier_feedback": (state.get("verification") or {}).get("failures")}
    out, upd = await _llm_json(state, ctx, "determine", state["variant"], data, "research", step, max_tokens=900)
    det = out if out and out.get("route") else rules.draft_determination(state["intake"], ev, state["complaint"])
    step["draft_source"] = "llm" if out and out.get("route") else "rules"
    # enrich citations with metadata from evidence (models often return only clause_id/passage_id/quote)
    by = {e["passage_id"]: e for e in ev} | {e["clause_id"]: e for e in ev}
    for c in det.get("citations") or []:
        e = by.get(c.get("passage_id")) or by.get(c.get("clause_id"))
        if e:
            c.setdefault("doc_id", e["doc_id"])
            c.setdefault("regulator", e["regulator"])
            c.setdefault("reference", e.get("reference", ""))
            c.setdefault("passage_id", e["passage_id"])
    if state.get("demo_fault") == "unsupported_citation" and state.get("attempt", 0) == 0:
        det = {**det, "citations": (det.get("citations") or []) + [{
            "clause_id": "SEBI-FAKE-99", "doc_id": "SEBI-SCORES-MC", "regulator": det.get("regulator") or "SEBI",
            "passage_id": "SEBI-SCORES-MC:SEBI-FAKE-99:0",
            "quote": "SEBI shall directly order the entity to pay compensation within 7 days."}]}
        step["demo_fault_injected"] = "unsupported_citation"
    step.update(route=det.get("route"), regulator=det.get("regulator"),
                citations=[c.get("clause_id") for c in det.get("citations") or []])
    return {**upd, "draft": det}


def _facts(state: RunState) -> list[str]:
    c, i = state["complaint"], state.get("intake") or {}
    return [c.get("complaint_text"), c.get("entity_name"), c.get("product"), i.get("product"), i.get("entity_name"),
            str(i.get("entity_type") or "").replace("_", " ")]


@node("verifier", "verify")
async def verify_node(state: RunState, ctx: RunContext, step: dict) -> dict:
    s = settings()
    attempt = state.get("attempt", 0) + 1
    try:
        v = await ctx.verify({"determination": state["draft"], "evidence": state.get("evidence") or [],
                              "intake": state.get("intake"), "facts": _facts(state)}, state["correlation_id"])
    except Exception as e:  # verifier agent down or timed out: unverified answers are never returned
        VERIFY_OUTCOMES.labels("verifier_unavailable").inc()
        AGENT_TASKS.labels("verifier", "failed").inc()
        step.update(outcome="verifier_unavailable", error=str(e)[:200])
        return {"attempt": attempt, "verification": {"verified": False, "failures": [
            {"code": "verifier_unavailable", "detail": str(e)[:200]}], "hints": []},
                "final": _escalate(state, "Verifier agent unavailable; the draft could not be verified.",
                                   status="degraded", provisional=state["draft"])}
    if v.get("usage", {}).get("prompt_tokens"):
        u = v["usage"]
        upd_usage = _add_usage(state, u["prompt_tokens"], u["completion_tokens"], u["cost_usd"])
    else:
        upd_usage = state.get("usage")
    refs = {**(state.get("prompt_refs") or {}), **({"verify_judge": v["prompt_ref"]} if v.get("prompt_ref") else {})}
    outcome = "verified" if v["verified"] else ("retry" if attempt < s.max_verify_attempts else "exhausted")
    VERIFY_OUTCOMES.labels(outcome).inc()
    AGENT_TASKS.labels("verifier", "ok").inc()
    step.update(outcome=outcome, verified=v["verified"], failures=v["failures"], hints=v.get("hints"),
                scores=v.get("scores"), llm_judge=v.get("llm_judge"))
    upd: dict = {"attempt": attempt, "verification": v, "usage": upd_usage, "prompt_refs": refs}
    if not v["verified"] and attempt < s.max_verify_attempts:
        # Retry with a different strategy: the verifier's hints become mandatory targeted lookups
        # (regulator-scoped scope search, clause-type filtered search, exact clause lookup).
        upd.update(verifier_hints=v.get("hints") or [], finished_research=False,
                   tool_bonus=state.get("tool_bonus", 0) + 3)  # grant a small extra tool budget for the retry
    return upd


@node("router", "finalize")
async def finalize(state: RunState, ctx: RunContext, step: dict) -> dict:
    if state.get("final"):
        step["outcome"] = state["final"]["status"]
        return {}
    intake, v, det = state.get("intake") or {}, state.get("verification") or {}, state.get("draft") or {}
    if state.get("stop_reason") and not det:
        final = _escalate(state, f"Loop bound reached ({state['stop_reason']}) before a determination.")
    elif not v.get("verified"):
        final = _escalate(state, "Self-verification failed after retries: "
                                 + "; ".join(f["code"] for f in v.get("failures", [])), provisional=det)
    elif intake.get("split"):
        final = _escalate(state, "The complaint splits across regulators (SEBI and RBI).", provisional=det)
    elif intake.get("out_of_corpus"):
        final = _escalate(state, det.get("reason") or "Entity type outside the corpus.", provisional=det)
    elif det.get("route") == "escalate_to_human":
        final = _escalate(state, det.get("reason") or "Determination requires a human.", provisional=det)
    else:
        final = {"status": "completed", "determination": det, "escalation": {"required": False, "path": None}}
    if state.get("degraded") and final["status"] == "completed":
        final = _escalate(state, "Degraded mode: the model provider was unavailable, so this rule-based routing "
                                 f"is provisional ({state.get('degraded_reason', '')[:120]}).",
                          status="degraded", provisional=det)
    ok, problems = guardrails.filter_output(final["determination"])
    if not ok and final["determination"]["route"] != "escalate_to_human":
        GUARDRAIL_EVENTS.labels("output_filter", "output").inc()
        final = _escalate(state, "Output filter blocked the determination: " + "; ".join(problems))
    step.update(outcome=final["status"], route=final["determination"]["route"], output_filter=problems)
    AGENT_TASKS.labels("router", final["status"]).inc()
    return {"final": final}


# ------------------------------------------------------------------ edges
def after_guard(state: RunState) -> str:
    return "finalize" if state.get("final") else "intake_agent"


def after_research(state: RunState) -> str:
    if state.get("final"):
        return "finalize"
    return "draft_determination" if state.get("finished_research") else "research_agent"


def after_verify(state: RunState) -> str:
    if state.get("final") or (state.get("verification") or {}).get("verified"):
        return "finalize"
    if state.get("attempt", 0) >= settings().max_verify_attempts or _over_budget(state):
        return "finalize"
    return "research_agent"


def build_graph():
    g = StateGraph(RunState)
    g.add_node("input_guard", input_guard)
    g.add_node("intake_agent", intake_agent)
    g.add_node("research_agent", research_agent)
    g.add_node("draft_determination", draft)
    g.add_node("verify", verify_node)
    g.add_node("finalize", finalize)
    g.set_entry_point("input_guard")
    g.add_conditional_edges("input_guard", after_guard, ["intake_agent", "finalize"])
    g.add_edge("intake_agent", "research_agent")
    g.add_conditional_edges("research_agent", after_research, ["research_agent", "draft_determination", "finalize"])
    g.add_edge("draft_determination", "verify")
    g.add_conditional_edges("verify", after_verify, ["research_agent", "finalize"])
    g.add_edge("finalize", END)
    return g.compile()


_graph = None


async def run_graph(ctx: RunContext, run_id: str, correlation_id: str, complaint: dict, variant: str,
                    demo_fault: str | None = None) -> dict:
    global _graph
    if _graph is None:
        _graph = build_graph()
    s = settings()
    init: RunState = {"run_id": run_id, "correlation_id": correlation_id, "complaint": complaint, "variant": variant,
                      "demo_fault": demo_fault if s.demo_faults_enabled else None, "started": time.time(),
                      "steps": 0, "tool_calls": 0, "attempt": 0, "evidence": [], "tool_history": [],
                      "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "llm_calls": 0},
                      "prompt_refs": {}}
    # recursion_limit is a hard backstop above the soft AGENT_MAX_STEPS bound
    return await _graph.ainvoke(init, config={"configurable": {"ctx": ctx}, "recursion_limit": s.max_steps * 3 + 10})
