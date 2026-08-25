"""Validate judge-owned stage results and aggregate run outcomes."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .contracts import ContractError, TaskSpec

STAGE_RESULT_SCHEMA = "kernelinfra.stage-result.v1"
RUN_RESULT_SCHEMA = "kernelinfra.run-result.v1"
STATUSES = frozenset({"passed", "failed"})
VALIDITIES = frozenset({"valid", "invalid", "unknown"})


def load_stage_result(path: Path, task: TaskSpec) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"judge result missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"judge result is invalid JSON: {exc}") from exc
    return validate_stage_result(value, task)


def validate_stage_result(value: Any, task: TaskSpec) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("stage result must be an object")
    allowed = {
        "schema",
        "status",
        "validity",
        "summary",
        "workloads",
        "artifacts",
        "fingerprints",
        "metrics",
    }
    unknown = sorted(value.keys() - allowed)
    missing = sorted({"schema", "status", "validity"} - value.keys())
    if missing:
        raise ContractError(f"stage result missing fields: {', '.join(missing)}")
    if unknown:
        raise ContractError(f"stage result unknown fields: {', '.join(unknown)}")
    if value["schema"] != STAGE_RESULT_SCHEMA:
        raise ContractError(f"stage result schema must be {STAGE_RESULT_SCHEMA!r}")
    if value["status"] not in STATUSES:
        raise ContractError("stage result status must be passed or failed")
    if value["validity"] not in VALIDITIES:
        raise ContractError("stage result validity must be valid, invalid, or unknown")
    if value["status"] == "passed" and value["validity"] == "invalid":
        raise ContractError("a passed stage cannot report invalid validity")
    if value["status"] == "failed" and value["validity"] == "valid":
        raise ContractError("a failed stage cannot report valid validity")
    if "summary" in value and not isinstance(value["summary"], str):
        raise ContractError("stage result summary must be a string")
    for field in ("artifacts", "fingerprints"):
        if field in value and not isinstance(value[field], dict):
            raise ContractError(f"stage result {field} must be an object")
    if "metrics" in value and not isinstance(value["metrics"], dict):
        raise ContractError("stage result metrics must be an object")

    workloads = value.get("workloads", [])
    if not isinstance(workloads, list):
        raise ContractError("stage result workloads must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(workloads):
        normalized.append(_validate_workload(row, index=index, task=task))
        workload_id = normalized[-1]["id"]
        if workload_id in seen:
            raise ContractError(f"duplicate workload result: {workload_id}")
        seen.add(workload_id)
    result = dict(value)
    result["workloads"] = normalized
    return result


def _validate_workload(
    value: Any, *, index: int, task: TaskSpec
) -> dict[str, Any]:
    where = f"stage result workloads[{index}]"
    if not isinstance(value, dict):
        raise ContractError(f"{where} must be an object")
    allowed = {
        "id",
        "correct",
        "candidate_ms",
        "baseline_ms",
        "candidate_samples_ms",
        "baseline_samples_ms",
        "stable",
        "speedup",
        "notes",
    }
    unknown = sorted(value.keys() - allowed)
    if unknown:
        raise ContractError(f"{where} unknown fields: {', '.join(unknown)}")
    workload_id = value.get("id")
    if workload_id not in task.workloads:
        raise ContractError(f"{where}.id is not declared by the task: {workload_id!r}")
    if "correct" in value and not isinstance(value["correct"], bool):
        raise ContractError(f"{where}.correct must be boolean")
    for field in ("candidate_ms", "baseline_ms"):
        if field in value:
            _finite_positive(value[field], f"{where}.{field}")
    if "speedup" in value:
        _finite_positive(value["speedup"], f"{where}.speedup")
    for field in ("candidate_samples_ms", "baseline_samples_ms"):
        if field in value:
            samples = value[field]
            if not isinstance(samples, list) or not samples:
                raise ContractError(f"{where}.{field} must be a non-empty list")
            for sample_index, sample in enumerate(samples):
                _finite_positive(sample, f"{where}.{field}[{sample_index}]")
    if "stable" in value and not isinstance(value["stable"], bool):
        raise ContractError(f"{where}.stable must be boolean")
    if "notes" in value and not isinstance(value["notes"], str):
        raise ContractError(f"{where}.notes must be a string")
    return dict(value)


def _finite_positive(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{where} must be a positive finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ContractError(f"{where} must be a positive finite number")
    return number


def aggregate_run_result(
    *,
    task: TaskSpec,
    run_id: str,
    task_digest: str,
    candidate_digest: str,
    stage_results: list[tuple[str, str, dict[str, Any]]],
    terminal_reason: str | None = None,
) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    fingerprints: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
    validity = "unknown"
    all_passed = bool(stage_results) and len(stage_results) == len(task.stages)
    stage_summaries: list[dict[str, Any]] = []

    for stage_id, stage_kind, result in stage_results:
        stage_summaries.append(
            {
                "id": stage_id,
                "kind": stage_kind,
                "status": result["status"],
                "validity": result["validity"],
                "summary": result.get("summary", ""),
            }
        )
        if result["status"] != "passed":
            all_passed = False
        if result["validity"] == "invalid":
            validity = "invalid"
        elif result["validity"] == "valid" and validity != "invalid":
            validity = "valid"
        elif result["validity"] == "unknown" and validity != "invalid":
            validity = "unknown"
        for row in result.get("workloads", []):
            merged.setdefault(row["id"], {}).update(row)
        fingerprints.update(result.get("fingerprints", {}))
        if result.get("metrics"):
            metrics[stage_id] = result["metrics"]

    timed = {
        workload_id
        for workload_id, row in merged.items()
        if "candidate_ms" in row and "baseline_ms" in row
    }
    stable = all(row.get("stable", True) for row in merged.values())
    frontier_eligible = (
        all_passed
        and validity == "valid"
        and set(task.workloads).issubset(timed)
        and stable
    )
    if all_passed:
        outcome = "completed"
    elif validity == "invalid":
        outcome = "rejected"
    else:
        outcome = "infra_error"

    return {
        "schema": RUN_RESULT_SCHEMA,
        "run_id": run_id,
        "task_id": task.task_id,
        "task_sha256": task_digest,
        "candidate_sha256": candidate_digest,
        "outcome": outcome,
        "validity": validity,
        "frontier_eligible": frontier_eligible,
        "terminal_reason": terminal_reason,
        "stages": stage_summaries,
        "workloads": [merged[key] for key in sorted(merged)],
        "fingerprints": fingerprints,
        "metrics": metrics,
    }
