"""Prompts are versioned artifacts on disk (L2). Every response records id@version#sha of each prompt used."""
from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path

import yaml

PROMPT_DIR = Path(__file__).parent / "prompts"
DIRS = {"intake": "intake", "planner": "planner", "determine": "determine", "verify_judge": "verify"}


@functools.lru_cache(maxsize=None)
def load(task: str, version: str) -> dict:
    d = PROMPT_DIR / DIRS[task]
    path = d / f"{version}.yaml"
    if not path.exists():  # tasks with a single version fall back to v1
        path = d / "v1.yaml"
    p = yaml.safe_load(path.read_text())
    p["sha"] = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    p["ref"] = f"{p['id']}@{p['version']}#{p['sha']}"
    return p


def render(task: str, version: str, data: dict) -> tuple[list[dict], str]:
    p = load(task, version)
    payload = json.dumps(data, default=str, ensure_ascii=False)
    fix = lambda s: s.replace("{{", "{").replace("}}", "}")
    msgs = [{"role": "system", "content": fix(p["system"])},
            {"role": "user", "content": fix(p["user"]).replace("{data}", payload)}]
    return msgs, p["ref"]
