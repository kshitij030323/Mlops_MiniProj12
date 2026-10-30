"""Deterministic domain rules.

Used in three places, never as the primary path when a real model is configured:
  * the LLM stub service (D6), which must return plausible fixed responses without a provider;
  * the degradation path (A5) when the provider circuit breaker is open: the result is then marked
    degraded and always escalated, never returned as a confident answer;
  * lexical priors in the intake agent (product -> entity -> regulator), which the model output is
    reconciled against.
"""
from __future__ import annotations

import datetime as dt
import re

SEBI_TYPES = {"broker", "mutual_fund", "registrar", "listed_company", "depository"}
RBI_TYPES = {"bank", "nbfc", "payment_system"}
OUT_OF_CORPUS = {"insurance": "IRDAI", "pension": "PFRDA", "chit_fund": "State Registrar of Chits",
                 "crypto": "no financial-sector regulator"}

LEXICON: dict[str, dict[str, float]] = {
    "broker": {"stock broker": 3, "stockbroker": 3, "broker": 2.5, "trading account": 2.5, "contract note": 3,
               "unauthorised trade": 3, "unauthorized trade": 3, "unauthorised trades": 3,
               "unauthorized trades": 3, "square off": 2.5, "squared off": 2.5, "margin": 1.5,
               "brokerage": 2, "intraday": 2, "f&o": 2, "futures and options": 2, "ledger balance": 2,
               "funds payout": 2, "payout": 1.5, "running account": 2, "sub-broker": 2,
               "authorised person": 1.5, "trading platform": 1.5},
    "mutual_fund": {"mutual fund": 3, "amc": 2.5, "asset management": 2.5, "nav": 2, "sip": 2,
                    "redemption": 2, "redeem": 2, "redeemed": 2, "units": 1.5, "idcw": 3, "folio": 2.5,
                    "exit load": 2.5, "elss": 2.5, "scheme": 1},
    "registrar": {"registrar": 3, "transfer agent": 3, "rta": 3, "share certificate": 2.5,
                  "share certificates": 2.5, "duplicate certificate": 3, "transmission": 2,
                  "physical shares": 2.5, "physical share": 2.5},
    "listed_company": {"listed company": 3, "dividend": 1.5, "rights issue": 2.5, "buyback": 2.5,
                       "buy-back": 2.5, "bonus shares": 2.5, "annual report": 2, "agm": 2, "shareholder": 1.5,
                       "debenture interest": 2.5, "ipo refund": 2, "allotment": 1},
    "depository": {"depository participant": 3, "demat": 2.5, "demat account": 3, "nsdl": 3, "cdsl": 3,
                   "delivery instruction": 3, "off-market transfer": 3, "off market transfer": 3,
                   "pledge": 2, "bo account": 2.5, "depository": 2.5},
    "bank": {"fixed deposit": 3.5, "recurring deposit": 3.5, "term deposit": 3, "savings account": 2.5,
             "current account": 2, "bank's app": 2.5, "banking app": 2.5, "net banking": 2.5,
             "netbanking": 2.5, "debit card": 3, "credit card": 3, "atm": 3, "upi": 2, "neft": 2, "imps": 2,
             "rtgs": 2, "cheque": 2, "locker": 3, "home loan": 3, "personal loan": 3, "emi": 2,
             "minimum balance": 3, "bank charges": 2.5, "bank": 0.75, "fd": 3},
    "nbfc": {"nbfc": 3, "non-banking financial": 3, "gold loan": 3, "loan app": 3, "microfinance": 2.5,
             "finance company": 2},
    "payment_system": {"wallet": 2, "prepaid card": 2, "payment app": 2, "payment gateway": 1.5},
    "insurance": {"insurance": 3, "insurer": 3, "claim settlement": 2.5, "irdai": 3, "lic": 2.5, "ulip": 2.5,
                  "premium": 1.5, "policy": 1},
    "pension": {"nps": 3, "pension": 2.5, "pfrda": 3, "epf": 3, "provident fund": 3},
    "chit_fund": {"chit fund": 3.5, "chit": 2.5},
    "crypto": {"crypto": 3.5, "cryptocurrency": 3.5, "bitcoin": 3.5, "usdt": 3, "crypto exchange": 3.5},
}
_LEX_RE = {t: [(re.compile(r"(?<![\w])" + re.escape(term) + r"(?![\w])", re.I), term, w)
               for term, w in terms.items()] for t, terms in LEXICON.items()}

CLAIMED_MAP = {"broker": "broker", "mutual_fund": "mutual_fund", "registrar": "registrar",
               "listed_company": "listed_company", "depository": "depository",
               "depository_participant": "depository", "bank": "bank", "nbfc": "nbfc", "insurance": "insurance"}
NAME_HINTS = [(r"\bbank\b", "bank", 2.0), (r"mutual fund|\bamc\b|asset management", "mutual_fund", 2.0),
              (r"securities|broking|stock ?brok", "broker", 1.5), (r"insurance|assurance", "insurance", 2.0),
              (r"registrar|transfer agent|\brta\b", "registrar", 2.0), (r"\bnbfc\b|finance ltd|fincorp", "nbfc", 1.5)]

DOMAIN_OF = {"broker": "broker", "mutual_fund": "mutual_fund", "registrar": "registrar",
             "listed_company": "listed_company", "depository": "depository"}
SPLIT_THRESHOLD = 2.5
GENERIC_ENTITY = {"broker": "the stock broker", "mutual_fund": "the asset management company",
                  "registrar": "the registrar and share transfer agent", "listed_company": "the listed company",
                  "depository": "the depository participant", "bank": "the bank", "nbfc": "the NBFC",
                  "payment_system": "the payment system operator"}


def score_entities(text: str, entity_name: str | None = None, claimed: str | None = None) -> dict:
    scores: dict[str, float] = {}
    hits: dict[str, list[str]] = {}
    for t, pats in _LEX_RE.items():
        for rx, term, w in pats:
            n = len(rx.findall(text or ""))
            if n:
                scores[t] = scores.get(t, 0) + w * min(n, 2)
                hits.setdefault(t, []).append(term)
    for rx, t, w in NAME_HINTS:
        if entity_name and re.search(rx, entity_name, re.I):
            scores[t] = scores.get(t, 0) + w
            hits.setdefault(t, []).append(f"entity_name:{entity_name}")
    if claimed and claimed in CLAIMED_MAP:
        t = CLAIMED_MAP[claimed]
        scores[t] = scores.get(t, 0) + 1.5
        hits.setdefault(t, []).append(f"claimed:{claimed}")
    return {"scores": {k: round(v, 2) for k, v in scores.items()}, "hits": hits}


def classify(c: dict) -> dict:
    """Rule-based intake: product -> entity type -> domain -> candidate regulators."""
    text = " ".join(filter(None, [c.get("complaint_text"), c.get("product")]))
    sc = score_entities(text, c.get("entity_name"), c.get("entity_type_claimed"))
    scores, hits = sc["scores"], sc["hits"]
    sebi = max([scores.get(t, 0) for t in SEBI_TYPES] or [0])
    rbi = max([scores.get(t, 0) for t in RBI_TYPES] or [0])
    ooc = max([scores.get(t, 0) for t in OUT_OF_CORPUS] or [0])
    top = max(scores, key=scores.get) if scores else None
    top_score = scores.get(top, 0) if top else 0

    candidate_regulators, split, out_of_corpus = [], False, False
    if sebi >= SPLIT_THRESHOLD:
        candidate_regulators.append("SEBI")
    if rbi >= SPLIT_THRESHOLD:
        candidate_regulators.append("RBI")
    if sebi >= SPLIT_THRESHOLD and rbi >= SPLIT_THRESHOLD:
        split = True
    if top in OUT_OF_CORPUS and ooc >= SPLIT_THRESHOLD:
        out_of_corpus = True
        candidate_regulators = [OUT_OF_CORPUS[top]]

    if top is None or top_score < 1.5:
        domain, entity_type = "unknown", "unknown"
    elif out_of_corpus or top in RBI_TYPES:
        domain, entity_type = "out_of_jurisdiction", top
    else:
        domain, entity_type = DOMAIN_OF.get(top, "unknown"), top
    if domain == "out_of_jurisdiction" and not out_of_corpus and not candidate_regulators:
        candidate_regulators = ["RBI"]
    if domain in DOMAIN_OF and "SEBI" not in candidate_regulators:
        candidate_regulators.insert(0, "SEBI")

    product = c.get("product") or (hits.get(top, [None])[0] if top else None)
    if product and str(product).startswith(("entity_name:", "claimed:")):
        product = None
    return {
        "product": product, "entity_type": entity_type, "domain": domain,
        "entity_name": c.get("entity_name") or GENERIC_ENTITY.get(entity_type),
        "candidate_regulators": candidate_regulators, "split": split, "out_of_corpus": out_of_corpus,
        "signals": hits, "scores": scores,
        "summary": f"Product '{product}' offered by a {entity_type} entity; regulators considered: "
                   f"{', '.join(candidate_regulators) or 'none'}",
    }


# ------------------------------------------------------------------ research policy
def _sig(tool: str, args: dict) -> str:
    return tool + ":" + ",".join(f"{k}={args[k]}" for k in sorted(args))


def planned_actions(state: dict) -> list[dict]:
    """The investigation plan: 1) what appears to apply, 2) what might modify it, 3) re-search from what was read."""
    intake = state.get("intake") or {}
    c = state.get("complaint") or {}
    ev = state.get("evidence_summary") or []
    acts: list[dict] = [
        {"tool": "get_complaint_field", "args": {"field": "entity_name"}},
        {"tool": "search_corpus", "args": {"query": (c.get("complaint_text") or "")[:300]}},
    ]
    first_regs = sorted({e["regulator"] for e in ev[:4]}) if ev else []
    # Step 2: does any other provision modify the one that appears to apply? Look at the scope of every
    # candidate regulator, and at the exclusions of whichever regulator the first search pointed to.
    for r in intake.get("candidate_regulators", []):
        if r in ("SEBI", "RBI"):
            acts.append({"tool": "list_regulator_scope", "args": {"regulator": r}})
    for r in first_regs:
        acts.append({"tool": "search_corpus", "args": {
            "query": f"complaints against entities not regulated by {r} or not within its regulatory purview",
            "regulator": r, "clause_type": "exclusion"}})
    # Step 3: derived queries from what has been read
    et = intake.get("entity_type")
    if et in SEBI_TYPES:
        acts.append({"tool": "search_corpus", "args": {
            "query": f"responsibility of {GENERIC_ENTITY[et]} for complaints {intake.get('product') or ''}".strip(),
            "regulator": "SEBI", "clause_type": "responsibility"}})
        acts.append({"tool": "search_corpus", "args": {
            "query": "timeline for redressal of complaint by the entity calendar days",
            "regulator": "SEBI", "clause_type": "timeline"}})
        if et == "mutual_fund":
            acts.append({"tool": "search_corpus", "args": {
                "query": "transfer of redemption proceeds IDCW working days interest for delay",
                "regulator": "SEBI", "clause_type": "timeline"}})
        acts.append({"tool": "search_corpus", "args": {
            "query": "time limit for lodging complaint three years cause of complaint",
            "regulator": "SEBI", "clause_type": "maintainability"}})
    if "RBI" in intake.get("candidate_regulators", []):
        acts.append({"tool": "search_corpus", "args": {
            "query": "customer aggrieved by act or omission of Regulated Entity deficiency in service",
            "regulator": "RBI"}})
        acts.append({"tool": "search_corpus", "args": {
            "query": "written complaint to the Regulated Entity before Ombudsman no reply within 30 days",
            "regulator": "RBI", "clause_type": "maintainability"}})
    for h in state.get("verifier_hints") or []:
        kind, _, val = h.partition(":")
        if kind == "missing_scope":
            acts.append({"tool": "list_regulator_scope", "args": {"regulator": val}})
        elif kind == "missing_type":
            typ, _, reg = val.partition("@")
            acts.append({"tool": "search_corpus", "args": {"query": f"{typ} provisions", "regulator": reg,
                                                           "clause_type": typ}})
        elif kind == "lookup_clause":
            acts.append({"tool": "get_clause", "args": {"clause_id": val}})
    return acts


def plan_next_action(state: dict) -> dict:
    done = {_sig(t["tool"], t["args"]) for t in state.get("tool_history") or []}
    budget = state.get("tool_budget_left", 99)
    if budget <= 0:
        return {"action": "finish", "args": {}, "thought": "tool budget exhausted"}
    for a in planned_actions(state):
        if _sig(a["tool"], a["args"]) not in done:
            return {"action": a["tool"], "args": a["args"],
                    "thought": f"next planned check: {a['tool']}"}
    return {"action": "finish", "args": {}, "thought": "all planned checks done"}


# ------------------------------------------------------------------ determination
_SENT = re.compile(r"(?<=[.;:])\s+")


def pick_quote(text: str, keywords: list[str]) -> str:
    sents = [s.strip() for s in _SENT.split(text) if len(s.strip()) > 25]
    if not sents:
        return text[:240]
    kw = [k.lower() for k in keywords]
    best = max(sents, key=lambda s: sum(k in s.lower() for k in kw))
    return best[:400]


def _find(evidence: list[dict], *, regulator=None, types=(), clause_ids=()) -> list[dict]:
    out = []
    for e in evidence:
        if regulator and e["regulator"] != regulator:
            continue
        if types and e["clause_type"] not in types:
            continue
        if clause_ids and e["clause_id"] not in clause_ids:
            continue
        out.append(e)
    return out


def _cite(e: dict, kws: list[str]) -> dict:
    return {"clause_id": e["clause_id"], "doc_id": e["doc_id"], "regulator": e["regulator"],
            "reference": e.get("reference", ""), "passage_id": e["passage_id"], "quote": pick_quote(e["text"], kws)}


def _dedupe(cites: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in cites:
        if c["clause_id"] not in seen:
            seen.add(c["clause_id"])
            out.append(c)
    return out


def draft_determination(intake: dict, evidence: list[dict], complaint: dict, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    et, domain = intake.get("entity_type"), intake.get("domain")
    entity = intake.get("entity_name") or GENERIC_ENTITY.get(et, "the entity")
    base = {"domain": domain, "entity_type": et, "responsible_entity": entity}

    if intake.get("out_of_corpus"):
        reg = (intake.get("candidate_regulators") or ["unknown"])[0]
        cites = [_cite(e, ["not regulated by SEBI", "appropriate regulator"])
                 for e in _find(evidence, clause_ids=("SCORES-2",))]
        return {**base, "route": "escalate_to_human", "regulator": None,
                "reason": f"The entity type '{et}' falls outside the indexed corpus (likely {reg}).",
                "basis": "Complaints against entities that are not regulated by SEBI are not dealt with under SCORES. "
                         f"The framework of the appropriate regulator ({reg}) is not in the corpus.",
                "citations": cites}
    if intake.get("split"):
        cites = [_cite(e, ["Regulated Entity", "registered intermediary"])
                 for e in (_find(evidence, regulator="SEBI", types=("scope", "responsibility"))[:1] +
                           _find(evidence, regulator="RBI", types=("scope", "grounds", "definition"))[:1])]
        return {**base, "route": "escalate_to_human", "regulator": None,
                "reason": "The complaint involves entities regulated by both SEBI and RBI.",
                "basis": "The complaint involves a SEBI registered intermediary and a Regulated Entity of the Reserve "
                         "Bank; each regulator's framework covers only its own entities.",
                "citations": _dedupe(cites)}
    if domain == "unknown":
        return {**base, "route": "escalate_to_human", "regulator": None,
                "reason": "The product and the responsible entity could not be established from the complaint.",
                "basis": "No provision could be tied to an identified entity.", "citations": []}

    if et in RBI_TYPES:
        scope = _find(evidence, regulator="RBI", types=("scope", "definition"))
        grounds = _find(evidence, regulator="RBI", types=("grounds",))
        maint = _find(evidence, regulator="RBI", clause_ids=("RBIOS-10B",))
        sebi_excl = _find(evidence, clause_ids=("SCORES-2",))
        cites = [_cite(e, ["Regulated Entity", "bank", "deficiency in service"]) for e in scope[:2]]
        cites += [_cite(e, ["aggrieved", "deficiency in service"]) for e in grounds[:1]]
        cites += [_cite(e, ["written complaint", "30 days"]) for e in maint[:1]]
        cites += [_cite(e, ["not regulated by SEBI", "appropriate regulator"]) for e in sebi_excl[:1]]
        return {**base, "route": "refer_to_other_regulator", "regulator": "RBI",
                "timeline": "The customer must first complain in writing to the Regulated Entity; the RBI "
                            "Ombudsman may be approached if there is no reply within 30 days or the reply is "
                            "unsatisfactory, within one year of the reply.",
                "basis": f"The product ({intake.get('product')}) is offered by {entity}, which is a Regulated "
                         "Entity of the Reserve Bank and not an entity regulated by SEBI. A shortfall in the "
                         "service the Regulated Entity is required to provide is a deficiency in service under "
                         "the Reserve Bank - Integrated Ombudsman Scheme, 2021.",
                "citations": _dedupe(cites)}

    # SEBI-regulated entity
    incident = complaint.get("incident_date")
    if isinstance(incident, str):
        incident = dt.date.fromisoformat(incident)
    limit = _find(evidence, clause_ids=("SCORES-4",))
    if incident and (today - incident).days > 3 * 365:
        return {**base, "route": "not_maintainable", "regulator": "SEBI",
                "reason": "Lodged more than three years after the date of the cause of the complaint.",
                "basis": "SCORES accepts a complaint only within three years from the date of the cause of "
                         "the complaint.",
                "citations": [_cite(e, ["three years"]) for e in limit[:1]]}
    resp = _find(evidence, regulator="SEBI", types=("responsibility",))
    resp = sorted(resp, key=lambda e: et not in e.get("entities", []))
    timeline = _find(evidence, clause_ids=("SCORES-5",))
    cites = [_cite(e, ["responsibility", "responsible"]) for e in resp[:1]]
    cites += [_cite(e, ["21 calendar days"]) for e in timeline[:1]]
    tl = "Entity to resolve and upload an Action Taken Report within 21 calendar days of receipt."
    if et == "mutual_fund":
        ctext = (complaint.get("complaint_text") or "").lower()
        key = "MFR-53B" if ("idcw" in ctext or "dividend" in ctext) else "MFR-53A"
        mf = _find(evidence, clause_ids=(key,))
        cites += [_cite(e, ["working days"]) for e in mf[:1]]
        if mf:
            tl += (" Statutory servicing obligation: IDCW within seven working days of the record date."
                   if key == "MFR-53B" else
                   " Statutory servicing obligation: redemption proceeds within three working days.")
    return {**base, "route": "assign_to_entity", "regulator": "SEBI", "timeline": tl,
            "basis": f"An investor may lodge a complaint on SCORES against {entity}, a {et.replace('_', ' ')} "
                     "regulated by SEBI, and complaints of this kind are the responsibility of that entity. "
                     "The entity shall resolve the complaint within 21 calendar days from the date of receipt.",
            "citations": _dedupe(cites)}
