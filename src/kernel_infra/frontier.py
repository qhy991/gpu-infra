"""Rebuildable per-workload frontier reduction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import TaskSpec
from .store import RunStore, utc_now

FRONTIER_SCHEMA = "kernelinfra.frontier.v1"


def rebuild_frontier(store: RunStore, task: TaskSpec) -> dict[str, Any]:
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for state in store.list_states(task_id=task.task_id):
        if state.get("task_sha256") != task.digest:
            continue
        result_path = store.run_dir(state["run_id"]) / "result.json"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        candidates.append((state, result))

    cells: dict[str, dict[str, Any]] = {}
    decisions: list[dict[str, Any]] = []
    seen_compiler: dict[str, str] = {}
    noise = task.comparison.relative_noise_floor

    for state, result in candidates:
        run_id = state["run_id"]
        rows = {row["id"]: row for row in result.get("workloads", [])}
        decision = "ineligible"
        reason = result.get("terminal_reason")
        if result.get("validity") == "valid" and any(
            row.get("stable") is False for row in rows.values()
        ):
            decision = "measurement-unstable"
            reason = "judge marked at least one workload measurement unstable"
        elif result.get("frontier_eligible"):
            compiler = _compiler_fingerprint(result)
            compiler_identity = (
                f"{compiler[0]}:{compiler[1]}" if compiler is not None else None
            )
            if compiler_identity is not None and compiler_identity in seen_compiler:
                decision = "compiler-equivalent"
                reason = (
                    f"same {compiler[0]} as {seen_compiler[compiler_identity]}"
                )
            else:
                guardrail_failure = _guardrail_failure(task, rows)
                if guardrail_failure is not None:
                    decision = "regresses-guardrail"
                    reason = guardrail_failure
                elif not cells:
                    decision = "first-valid"
                else:
                    improved = []
                    regressed = []
                    for workload_id in task.comparison.primary_workloads:
                        current = cells.get(workload_id)
                        if current is None:
                            improved.append(workload_id)
                            continue
                        candidate_ms = float(rows[workload_id]["candidate_ms"])
                        best_ms = float(current["candidate_ms"])
                        if candidate_ms < best_ms * (1 - noise):
                            improved.append(workload_id)
                        elif candidate_ms > best_ms * (1 + noise):
                            regressed.append(workload_id)
                    if improved and regressed:
                        decision = "per-cell-pareto"
                        reason = (
                            "improves "
                            + ",".join(improved)
                            + "; regresses "
                            + ",".join(regressed)
                        )
                    elif improved:
                        decision = "improves-primary"
                        reason = "improves " + ",".join(improved)
                    else:
                        decision = "dominated"
                        reason = "no primary workload improved beyond the noise floor"

                if decision in {"first-valid", "improves-primary", "per-cell-pareto"}:
                    for workload_id, row in rows.items():
                        current = cells.get(workload_id)
                        candidate_ms = float(row["candidate_ms"])
                        if current is None or candidate_ms < float(current["candidate_ms"]):
                            baseline_ms = float(row["baseline_ms"])
                            cells[workload_id] = {
                                "run_id": run_id,
                                "candidate_ms": candidate_ms,
                                "baseline_ms": baseline_ms,
                                "speedup": baseline_ms / candidate_ms,
                            }
                if compiler_identity is not None:
                    seen_compiler.setdefault(compiler_identity, run_id)

        decisions.append(
            {
                "run_id": run_id,
                "decision": decision,
                "reason": reason,
                "validity": result.get("validity", "unknown"),
                "outcome": result.get("outcome", "unknown"),
            }
        )

    projection = {
        "schema": FRONTIER_SCHEMA,
        "task_id": task.task_id,
        "task_sha256": task.digest,
        "generated_at": utc_now(),
        "relative_noise_floor": noise,
        "cells": cells,
        "decisions": decisions,
    }
    path = store.frontiers_dir / task.task_id / f"{task.digest}.json"
    store.atomic_json(path, projection)
    projection["path"] = str(path)
    return projection


def _compiler_fingerprint(result: dict[str, Any]) -> tuple[str, str] | None:
    fingerprints = result.get("fingerprints", {})
    if not isinstance(fingerprints, dict):
        return None
    for key in ("cubin_sha256", "sass_sha256", "binary_sha256"):
        value = fingerprints.get(key)
        if isinstance(value, str) and value:
            return key, value
    return None


def _guardrail_failure(
    task: TaskSpec, rows: dict[str, dict[str, Any]]
) -> str | None:
    for workload_id, threshold in task.comparison.guardrails.items():
        row = rows[workload_id]
        speedup = float(row["baseline_ms"]) / float(row["candidate_ms"])
        if speedup < threshold:
            return (
                f"{workload_id} speedup {speedup:.6f} is below "
                f"guardrail {threshold:.6f}"
            )
    return None
