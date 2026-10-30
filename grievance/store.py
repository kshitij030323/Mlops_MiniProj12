"""Redis-backed state: run queue, run records, step traces, quarantine, feedback, caches, rate limits.

Runs are also written to DATA_DIR/runs/<run_id>.json when they finish, so a run is reconstructible even if
Redis is flushed (D5).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import redis.asyncio as aioredis

from .config import settings

QUEUE = "queue:runs"
_r: aioredis.Redis | None = None


def r() -> aioredis.Redis:
    global _r
    if _r is None:
        _r = aioredis.from_url(settings().redis_url, decode_responses=True)
    return _r


def _dump(o) -> str:
    return json.dumps(o, default=str)


async def enqueue(job: dict) -> None:
    await r().lpush(QUEUE, _dump(job))


async def dequeue(timeout: int = 5) -> dict | None:
    item = await r().brpop(QUEUE, timeout=timeout)
    return json.loads(item[1]) if item else None


async def queue_depth() -> int:
    return await r().llen(QUEUE)


async def save_run(run_id: str, rec: dict) -> None:
    await r().set(f"run:{run_id}", _dump(rec), ex=7 * 86400)


async def get_run(run_id: str) -> dict | None:
    v = await r().get(f"run:{run_id}")
    return json.loads(v) if v else None


async def update_run(run_id: str, **fields) -> dict:
    rec = await get_run(run_id) or {}
    rec.update(fields)
    await save_run(run_id, rec)
    return rec


async def add_step(run_id: str, step: dict) -> None:
    step.setdefault("ts", time.time())
    await r().rpush(f"steps:{run_id}", _dump(step))
    await r().expire(f"steps:{run_id}", 7 * 86400)


async def get_steps(run_id: str) -> list[dict]:
    return [json.loads(s) for s in await r().lrange(f"steps:{run_id}", 0, -1)]


def persist_run_file(run_id: str, rec: dict, steps: list[dict]) -> None:
    d = Path(settings().data_dir) / "runs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.json").write_text(json.dumps({"run": rec, "steps": steps}, indent=1, default=str))


def read_run_file(run_id: str) -> dict | None:
    p = Path(settings().data_dir) / "runs" / f"{run_id}.json"
    return json.loads(p.read_text()) if p.exists() else None


async def quarantine(record: dict) -> None:
    await r().lpush("quarantine", _dump(record))
    await r().ltrim("quarantine", 0, 9999)
    d = Path(settings().data_dir) / "quarantine"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "quarantine.jsonl", "a") as f:
        f.write(_dump(record) + "\n")


async def list_quarantine(n: int = 50) -> list[dict]:
    return [json.loads(x) for x in await r().lrange("quarantine", 0, n - 1)]


async def add_feedback(run_id: str, fb: dict) -> None:
    await r().rpush("feedback", _dump({"run_id": run_id, **fb}))
    await r().hincrby("feedback:agg", "satisfactory" if fb["satisfactory"] else "unsatisfactory", 1)
    d = Path(settings().data_dir) / "feedback"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "feedback.jsonl", "a") as f:
        f.write(_dump({"run_id": run_id, **fb}) + "\n")


async def feedback_summary() -> dict:
    agg = await r().hgetall("feedback:agg")
    items = [json.loads(x) for x in await r().lrange("feedback", -200, -1)]
    s, u = int(agg.get("satisfactory", 0)), int(agg.get("unsatisfactory", 0))
    reasons: dict[str, int] = {}
    for i in items:
        if not i["satisfactory"]:
            reasons[i["reason"][:60]] = reasons.get(i["reason"][:60], 0) + 1
    return {"satisfactory": s, "unsatisfactory": u, "satisfaction_rate": (s / (s + u)) if s + u else None,
            "top_unsatisfactory_reasons": sorted(reasons.items(), key=lambda x: -x[1])[:10]}


async def add_quality(sample: dict) -> None:
    await r().rpush("quality", _dump(sample))
    await r().ltrim("quality", -2000, -1)


async def quality_series(n: int = 200) -> list[dict]:
    return [json.loads(x) for x in await r().lrange("quality", -n, -1)]


async def cache_get(key: str):
    v = await r().get(f"cache:{key}")
    return json.loads(v) if v else None


async def cache_set(key: str, val, ttl: int) -> None:
    await r().set(f"cache:{key}", _dump(val), ex=ttl)


async def rate_limit_ok(client: str, limit_per_min: int) -> tuple[bool, int]:
    """Fixed one-minute window per client."""
    k = f"rl:{client}:{int(time.time() // 60)}"
    n = await r().incr(k)
    if n == 1:
        await r().expire(k, 65)
    return n <= limit_per_min, n
