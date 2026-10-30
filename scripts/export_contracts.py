"""Exports the published data contracts (JSON Schema) to contracts/."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from grievance.contracts import ComplaintRequest, Feedback, RunAccepted, RunResult  # noqa: E402

out = Path(__file__).resolve().parent.parent / "contracts"
for name, model in {"complaint_request": ComplaintRequest, "run_accepted": RunAccepted, "run_result": RunResult,
                    "feedback": Feedback}.items():
    (out / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), indent=1))
    print("wrote", out / f"{name}.schema.json")
