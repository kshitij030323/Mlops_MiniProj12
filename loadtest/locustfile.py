"""Locust load test (D6). Measures a COMPLETE run - submission to final result - not just the submit call.

    locust -f loadtest/locustfile.py --host http://localhost:8000 --headless -u 20 -r 2 -t 3m \
        --csv loadtest/results/u20

Each simulated user submits a complaint, then polls until the run is terminal and reports a custom
"RUN complete_run" event with the end-to-end latency. Caching is disabled per request (use_cache=false) unless
LOCUST_USE_CACHE=true, so the test measures the system rather than the run cache. Each user has its own
X-Client-Id, so the per-client rate limit does not distort the result (set RATE_LIMIT_PER_MIN high for the test).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import time
import uuid
from pathlib import Path

from locust import HttpUser, between, events, task

SCEN = [json.loads(l) for l in (Path(__file__).resolve().parent.parent / "eval" / "scenarios.jsonl").read_text().splitlines()
        if l.strip()]
SCEN = [s for s in SCEN if s["id"] not in ("S20_injection",)]
USE_CACHE = os.getenv("LOCUST_USE_CACHE", "false").lower() == "true"
POLL_S = float(os.getenv("LOCUST_POLL_S", "0.5"))
MAX_WAIT_S = float(os.getenv("LOCUST_MAX_WAIT_S", "240"))


class Investor(HttpUser):
    wait_time = between(0.5, 1.5)

    def on_start(self):
        self.client_id = f"locust-{uuid.uuid4().hex[:8]}"

    @task
    def complete_run(self):
        s = random.choice(SCEN)
        body = json.loads(json.dumps(s["request"]))
        body["incident_date"] = (dt.date.today() - dt.timedelta(days=s["incident_days_ago"])).isoformat()
        body["options"] = {"use_cache": USE_CACHE}
        t0 = time.perf_counter()
        with self.client.post("/v1/complaints", json=body, headers={"X-Client-Id": self.client_id},
                              name="POST /v1/complaints", catch_response=True) as r:
            if r.status_code != 202:
                r.failure(f"submit {r.status_code}")
                return
            run_id = r.json()["run_id"]
        status = "queued"
        while time.perf_counter() - t0 < MAX_WAIT_S:
            g = self.client.get(f"/v1/runs/{run_id}", name="GET /v1/runs/{id}")
            status = g.json().get("status") if g.status_code == 200 else "error"
            if status in ("completed", "escalated", "degraded", "failed"):
                break
            time.sleep(POLL_S)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        exc = None if status in ("completed", "escalated") else Exception(f"run ended {status}")
        events.request.fire(request_type="RUN", name="complete_run", response_time=elapsed_ms, response_length=0,
                            exception=exc, context={})
