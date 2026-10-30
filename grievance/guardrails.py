"""Guardrails (L4): prompt-injection defence, toxicity detection, and output filtering.

Layered defence, because injected text can reach a model that holds tools:
 1. Detection: pattern + heuristic score on every free-text field before any model sees it.
 2. Isolation: free text is passed to models only inside <complaint> delimiters, declared as data, and with
    delimiter look-alikes stripped (sanitize()).
 3. Least privilege: every tool is read-only; no tool can change a route, close a complaint or send anything.
 4. Output filter: the final determination is checked against an allow-list of routes/regulators and a
    deny-list of determinations the system is not authorised to make (dispose, award, penalise...).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"disregard (all |the )?(previous|prior|above|system)",
    r"forget (everything|all|your) (instructions|rules|previous)",
    r"\byou are now\b", r"\bact as (an? )?(admin|administrator|developer|system|sebi officer)",
    r"system prompt", r"\bdeveloper mode\b", r"\bjailbreak\b", r"\bDAN\b",
    r"reveal (your|the) (prompt|instructions|system)",
    r"</?\s*(system|assistant|complaint|instructions?)\s*>",
    r"\b(route|assign|mark|classify) (this|the complaint|it) (as|to) ",
    r"\bmark (this|it) as (resolved|closed|disposed)",
    r"\b(call|invoke|use) the (tool|function)\b",
    r"\"(tool|action|route)\"\s*:",
    r"\bnew instructions?\b", r"\boverride\b.*\b(policy|rules|instructions)",
    r"\bprint (all|the) (passages|documents|corpus)\b",
]
_INJ = [re.compile(p, re.I) for p in INJECTION_PATTERNS]

# Small lexicon-based toxicity detector. Deliberately conservative: grievances are often angry, and anger is
# not toxicity. We flag abuse/threats/slurs; we mask them before any model call and never echo them.
TOXIC_TERMS = [
    "idiot", "idiots", "stupid", "moron", "bastard", "bloody fool", "scum", "f***", "fuck", "fucking", "shit",
    "bitch", "asshole", "chutiya", "harami", "kamina", "saala",
]
THREAT_PATTERNS = [r"\b(i will|i'll|we will) (kill|hurt|attack|burn|bomb)\b", r"\bkill (you|them|him|her)\b",
                   r"\bbomb (the|your)\b"]
_TOX = [re.compile(r"\b" + re.escape(t) + r"\b", re.I) for t in TOXIC_TERMS]
_THREAT = [re.compile(p, re.I) for p in THREAT_PATTERNS]


@dataclass
class GuardResult:
    injection_score: float = 0.0
    injection_hits: list[str] = field(default_factory=list)
    toxicity_score: float = 0.0
    toxicity_hits: list[str] = field(default_factory=list)
    threat: bool = False

    @property
    def injection_blocked(self) -> bool:
        return self.injection_score >= 0.5

    def as_dict(self) -> dict:
        return {"injection_score": round(self.injection_score, 2), "injection_hits": self.injection_hits,
                "injection_blocked": self.injection_blocked, "toxicity_score": round(self.toxicity_score, 2),
                "toxicity_hits": self.toxicity_hits, "threat": self.threat}


def scan(text: str) -> GuardResult:
    r = GuardResult()
    for p in _INJ:
        m = p.search(text or "")
        if m:
            r.injection_hits.append(m.group(0)[:60])
    # One strong pattern is enough to block; weak signals add up.
    r.injection_score = min(1.0, 0.5 * len(r.injection_hits))
    words = max(1, len((text or "").split()))
    for p in _TOX:
        r.toxicity_hits += [m.group(0) for m in p.finditer(text or "")]
    r.threat = any(p.search(text or "") for p in _THREAT)
    r.toxicity_score = min(1.0, 5 * len(r.toxicity_hits) / words + (0.8 if r.threat else 0))
    return r


def scan_fields(fields: dict[str, str | None]) -> dict[str, GuardResult]:
    return {k: scan(v) for k, v in fields.items() if v}


def sanitize(text: str) -> str:
    """Neutralise delimiter look-alikes and mask abusive terms before the text reaches a model."""
    t = re.sub(r"</?\s*(system|assistant|user|complaint|instructions?|data)\s*>", "[removed-tag]", text or "",
               flags=re.I)
    t = t.replace("<<", "« ").replace(">>", " »")
    for p in _TOX:
        t = p.sub("[masked]", t)
    return t


# ----------------------------------------------------------------- output filter
ALLOWED_ROUTES = {"assign_to_entity", "refer_to_other_regulator", "not_maintainable", "escalate_to_human"}
CORPUS_REGULATORS = {"SEBI", "RBI"}
FORBIDDEN_OUTPUT = [
    r"\b(complaint|case|grievance) (is|has been|stands) (closed|resolved|disposed|dismissed|rejected)\b",
    r"\b(award|compensation|refund|penalty|fine) (of|is|has been) (granted|awarded|imposed|ordered)\b",
    r"\bwe (order|direct|award|penali[sz]e)\b", r"\bguarantee(d)? (refund|compensation)\b",
    r"\bfinal (decision|order|disposal)\b",
]
_FORBID = [re.compile(p, re.I) for p in FORBIDDEN_OUTPUT]


def filter_output(det: dict) -> tuple[bool, list[str]]:
    """Return (ok, problems). The system may only *recommend* a routing; it may not dispose or award."""
    problems = []
    if det.get("route") not in ALLOWED_ROUTES:
        problems.append(f"route '{det.get('route')}' not in allowed set")
    if det.get("route") in {"assign_to_entity", "refer_to_other_regulator"} and \
            det.get("regulator") not in CORPUS_REGULATORS:
        problems.append(f"regulator '{det.get('regulator')}' is outside the corpus")
    text = " ".join(str(det.get(k) or "") for k in ("basis", "reason", "timeline", "responsible_entity"))
    for p in _FORBID:
        if p.search(text):
            problems.append(f"unauthorised determination language: '{p.pattern}'")
    tox = scan(text)
    if tox.toxicity_score > 0 or tox.injection_hits:
        problems.append("toxic or injected content in output")
    return (not problems), problems
