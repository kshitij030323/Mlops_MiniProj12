"""Deterministic verification of a draft determination against retrieved passages (A3).

Shared by the verifier agent service and the evaluation harness, so the metrics we report are the checks we run.
"""
from __future__ import annotations

import re

_W = re.compile(r"[a-z0-9]+")
STOP = set("""a an the of to in on for by and or is are was were be been with as at from that this which it its
shall may any such not no under where who whom their there than then into within other per has have had
""".split())


def norm(t: str) -> str:
    return " ".join(_W.findall((t or "").lower()))


def content_tokens(t: str) -> set[str]:
    return {w for w in _W.findall((t or "").lower()) if w not in STOP and len(w) > 2}


def support(claim: str, passages: list[str]) -> float:
    """Fraction of a claim's content tokens that appear in the retrieved passages."""
    ct = content_tokens(claim)
    if not ct:
        return 1.0
    pool = set().union(*[content_tokens(p) for p in passages]) if passages else set()
    return len(ct & pool) / len(ct)


def split_claims(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;])\s+", text or "") if len(s.strip()) > 15]


REQUIRED = {
    # route -> list of (requirement code, predicate over citations, hint)
    "refer_to_other_regulator": [
        ("scope_of_target_regulator",
         lambda d, c: any(x["regulator"] == d.get("regulator") and x["_type"] in ("scope", "definition", "grounds")
                          for x in c), lambda d: f"missing_scope:{d.get('regulator')}"),
    ],
    "assign_to_entity": [
        ("entity_responsibility", lambda d, c: any(x["_type"] == "responsibility" for x in c),
         lambda d: "missing_type:responsibility@SEBI"),
        ("redressal_timeline", lambda d, c: any(x["_type"] == "timeline" for x in c),
         lambda d: "missing_type:timeline@SEBI"),
    ],
    "not_maintainable": [
        ("maintainability_provision", lambda d, c: any(x["_type"] == "maintainability" for x in c),
         lambda d: f"missing_type:maintainability@{d.get('regulator') or 'SEBI'}"),
    ],
}

GROUNDEDNESS_THRESHOLD = 0.6


def verify(determination: dict, evidence: list[dict], facts: list[str] | None = None) -> dict:
    """Every citation must resolve to retrieved text; the route must have the provisions it depends on;
    the basis must be grounded in the cited passages (plus facts stated in the complaint itself, e.g. the
    product and entity name - those are grounded in the request, not in the regulations)."""
    failures, hints = [], []
    by_pid = {e["passage_id"]: e for e in evidence}
    by_cid = {}
    for e in evidence:
        by_cid.setdefault(e["clause_id"], e)
    resolved = []
    for c in determination.get("citations") or []:
        e = by_pid.get(c.get("passage_id")) or by_cid.get(c.get("clause_id"))
        if not e:
            failures.append({"code": "citation_not_retrieved", "detail": c.get("clause_id")})
            hints.append(f"lookup_clause:{c.get('clause_id')}")
            continue
        if norm(c.get("quote", "")) not in norm(e["text"]):
            failures.append({"code": "quote_not_in_passage", "detail": c.get("clause_id")})
            continue
        if c.get("regulator") and c["regulator"] != e["regulator"]:
            failures.append({"code": "citation_regulator_mismatch", "detail": c.get("clause_id")})
            continue
        resolved.append({**c, "_type": e["clause_type"], "regulator": e["regulator"], "_text": e["text"]})

    route = determination.get("route")
    if route != "escalate_to_human":
        if not determination.get("citations"):
            failures.append({"code": "no_citations", "detail": route})
        for code, pred, hint in REQUIRED.get(route, []):
            if not pred(determination, resolved):
                failures.append({"code": f"missing_{code}", "detail": route})
                hints.append(hint(determination))
        if route in ("refer_to_other_regulator", "assign_to_entity"):
            if not any(x["regulator"] == determination.get("regulator") for x in resolved):
                failures.append({"code": "no_citation_from_routed_regulator", "detail": determination.get("regulator")})
                hints.append(f"missing_scope:{determination.get('regulator')}")

    cited_text = [x["_text"] for x in resolved] + [f for f in (facts or []) if f]
    claims = split_claims(determination.get("basis", ""))
    claim_scores = [support(cl, cited_text) for cl in claims]
    groundedness = sum(claim_scores) / len(claim_scores) if claim_scores else 1.0
    if route != "escalate_to_human" and groundedness < GROUNDEDNESS_THRESHOLD:
        # Name the weakly supported sentences so a redraft can fix them (otherwise the model repeats itself).
        weak = [cl for cl, sc in zip(claims, claim_scores) if sc < GROUNDEDNESS_THRESHOLD]
        failures.append({"code": "basis_not_grounded", "detail": round(groundedness, 2), "weak_claims": weak[:3]})

    n = len(determination.get("citations") or [])
    return {
        "verified": not failures,
        "failures": failures,
        "hints": sorted(set(hints)),
        "scores": {"citation_resolution": (len(resolved) / n) if n else 1.0, "groundedness": round(groundedness, 3)},
        "resolved_citations": [x["clause_id"] for x in resolved],
    }
