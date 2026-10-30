# Service level objective (declared before any load test)

**Declared:** 2026-10-29, before the first Locust run. The numbers in `docs/LOADTEST.md` were measured afterwards against this target. The target was not changed after seeing them.

## SLO

> **p95 end-to-end run latency ≤ 30 s** (from `POST /v1/complaints` to a terminal status on `GET /v1/runs/{id}`), at a sustained load of **≥ 0.5 completed runs/s**, with **≥ 99 % of runs reaching a terminal status** (completed, escalated, or explicitly degraded) and no run returned as a confident answer without verification.

## Justification against section 1

* The regulatory clock is measured in days. Under SCORES an entity has 21 calendar days to redress a complaint, and under RB-IOS the customer must wait 30 days for the bank's reply. Routing is a triage step in front of that clock. A few seconds more or less does not change the investor's redress time. A *misrouted* complaint does: it adds weeks.
* So the SLO is generous on latency and strict on correctness. A slower, verified answer beats a fast unverified one, and escalation always counts as success.
* 30 s still bounds the experience of the web client, where a person submits and watches the result. It is also inside common reverse-proxy idle timeouts for the polling client.
* Budget: the agent makes about 10 sequential model calls per run. The load-test stub answers in 1.2 s ± 30 % per call, so an unloaded run takes about 12–14 s. 30 s leaves about 2× headroom for queueing before the target is breached.
* 0.5 runs/s is about 43,000 complaints/day, well above SCORES' daily intake. It is a capacity target for one laptop running Docker Compose, not a production sizing.
