"""Read-only tools the research agent may choose to call (A1). No tool can change a route or contact anyone."""
from __future__ import annotations

import asyncio

from ..observability import RETRIEVAL_CACHE
from ..retrieval import cache_key

TOOL_SPECS = {
    "search_corpus": "search_corpus(query, regulator?, clause_type?)",
    "get_clause": "get_clause(clause_id)",
    "get_complaint_field": "get_complaint_field(field)",
    "list_regulator_scope": "list_regulator_scope(regulator)",
}
READABLE_FIELDS = {"complaint_text", "entity_name", "entity_type_claimed", "product", "amount_inr", "incident_date",
                   "complained_to_entity_on", "entity_response_received", "entity_response_date",
                   "regulator_addressed", "channel"}


async def run_tool(ctx, state: dict, tool: str, args: dict) -> dict:
    """Returns {"passages": [...], "value": ...}. Raises ValueError for an unknown tool or bad args."""
    if tool == "get_complaint_field":
        f = args.get("field")
        if f not in READABLE_FIELDS:
            raise ValueError(f"field '{f}' is not readable")
        return {"passages": [], "value": state["complaint"].get(f)}
    rt = ctx.retriever
    if tool == "search_corpus":
        q = str(args.get("query") or "")[:500]
        if not q.strip():
            raise ValueError("empty query")
        reg, ct = args.get("regulator"), args.get("clause_type")
        key = cache_key("search", q, ctx.k, reg, ct, rt.version)
        cached = await ctx.cache_get(key)
        if cached is not None:
            RETRIEVAL_CACHE.labels("hit").inc()
            return {"passages": cached, "cached": True}
        RETRIEVAL_CACHE.labels("miss").inc()
        res = await asyncio.to_thread(rt.search, q, ctx.k, reg, ct)
        await ctx.cache_set(key, res)
        return {"passages": res, "cached": False}
    if tool == "get_clause":
        p = rt.get_clause(str(args.get("clause_id")))
        return {"passages": [p] if p else [], "value": None if p else "not found"}
    if tool == "list_regulator_scope":
        reg = args.get("regulator")
        if reg not in ("SEBI", "RBI"):
            return {"passages": [], "value": f"regulator {reg} is not in the corpus"}
        return {"passages": rt.scope(reg)}
    raise ValueError(f"unknown tool '{tool}'")
