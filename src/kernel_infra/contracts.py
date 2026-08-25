"""Strict, dependency-free task contract parsing."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASK_SCHEMA = "kernelinfra.task.v1"
STAGE_KINDS = frozenset(
    {"compile", "correctness", "sanitize", "benchmark", "profile", "judge"}
)
MODES = frozenset({"shared", "exclusive"})
EXECUTIONS = frozenset({"broker", "local", "service"})
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")


class ContractError(ValueError):
    """A task or result violates its declared schema."""


def _expect_object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{where} must be an object")
    return value


def _strict_keys(
    value: dict[str, Any], *, required: set[str], optional: set[str], where: str
) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required - optional)
    if missing:
        raise ContractError(f"{where} missing fields: {', '.join(missing)}")
    if unknown:
        raise ContractError(f"{where} unknown fields: {', '.join(unknown)}")


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{where} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, where: str) -> str:
    result = _text(value, where)
    if not _ID.fullmatch(result):
        raise ContractError(
            f"{where} must match {_ID.pattern!r}; got {result!r}"
        )
    return result


def _positive_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{where} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ContractError(f"{where} must be a finite positive number")
    return result


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError(f"{where} must be a positive integer")
    return value


def _optional_timeout(value: Any, where: str) -> float | None:
    if value is None:
        return None
    return _positive_number(value, where)


@dataclass(frozen=True)
class ResourceSpec:
    mode: str
    gpu_count: int
    estimate_s: float | None
    queue_timeout_s: float | None
    run_timeout_s: float


@dataclass(frozen=True)
class StageSpec:
    id: str
    kind: str
    execution: str
    judge_identity: str
    cwd: Path
    command: tuple[str, ...]
    resources: ResourceSpec | None
    service_deployment_id: str | None


@dataclass(frozen=True)
class ComparisonSpec:
    primary_workloads: tuple[str, ...]
    guardrails: dict[str, float]
    relative_noise_floor: float


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    description: str
    workloads: tuple[str, ...]
    comparison: ComparisonSpec
    stages: tuple[StageSpec, ...]
    raw: dict[str, Any]
    digest: str
    source_path: Path


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def load_task(path: Path) -> TaskSpec:
    source = path.expanduser().resolve()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"task file not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid task JSON at {source}: {exc}") from exc
    return parse_task(raw, source_path=source)


def parse_task(raw_value: Any, *, source_path: Path) -> TaskSpec:
    raw = _expect_object(raw_value, "task")
    _strict_keys(
        raw,
        required={"schema", "task_id", "workloads", "comparison", "stages"},
        optional={"description"},
        where="task",
    )
    if raw["schema"] != TASK_SCHEMA:
        raise ContractError(
            f"task.schema must be {TASK_SCHEMA!r}; got {raw['schema']!r}"
        )
    task_id = _identifier(raw["task_id"], "task.task_id")
    description = str(raw.get("description", "")).strip()

    workloads_value = raw["workloads"]
    if not isinstance(workloads_value, list) or not workloads_value:
        raise ContractError("task.workloads must be a non-empty string list")
    workloads = tuple(
        _identifier(value, f"task.workloads[{index}]")
        for index, value in enumerate(workloads_value)
    )
    if len(set(workloads)) != len(workloads):
        raise ContractError("task.workloads must be unique")

    comparison_raw = _expect_object(raw["comparison"], "task.comparison")
    _strict_keys(
        comparison_raw,
        required={"primary_workloads", "relative_noise_floor"},
        optional={"guardrails"},
        where="task.comparison",
    )
    primary_value = comparison_raw["primary_workloads"]
    if not isinstance(primary_value, list) or not primary_value:
        raise ContractError(
            "task.comparison.primary_workloads must be a non-empty list"
        )
    primary = tuple(
        _identifier(value, f"task.comparison.primary_workloads[{index}]")
        for index, value in enumerate(primary_value)
    )
    unknown_primary = sorted(set(primary) - set(workloads))
    if unknown_primary:
        raise ContractError(
            "primary workloads are not declared by task: "
            + ", ".join(unknown_primary)
        )
    noise = comparison_raw["relative_noise_floor"]
    if isinstance(noise, bool) or not isinstance(noise, (int, float)):
        raise ContractError("relative_noise_floor must be a number in [0, 1)")
    noise = float(noise)
    if not math.isfinite(noise) or not 0 <= noise < 1:
        raise ContractError("relative_noise_floor must be a number in [0, 1)")

    guardrails_raw = comparison_raw.get("guardrails", {})
    if not isinstance(guardrails_raw, dict):
        raise ContractError(
            "task.comparison.guardrails must map workload ids to minimum speedup"
        )
    guardrails: dict[str, float] = {}
    for workload_id, threshold in guardrails_raw.items():
        workload = _identifier(workload_id, "guardrail workload")
        if workload not in workloads:
            raise ContractError(f"guardrail workload is not declared: {workload}")
        guardrails[workload] = _positive_number(
            threshold, f"guardrail {workload}"
        )

    stages_value = raw["stages"]
    if not isinstance(stages_value, list) or not stages_value:
        raise ContractError("task.stages must be a non-empty list")
    stages = tuple(
        _parse_stage(value, index=index, task_dir=source_path.parent)
        for index, value in enumerate(stages_value)
    )
    if len({stage.id for stage in stages}) != len(stages):
        raise ContractError("task stage ids must be unique")
    if not any(stage.kind in {"correctness", "judge"} for stage in stages):
        raise ContractError("task must contain a correctness or judge stage")
    for stage in stages:
        if (
            stage.execution == "broker"
            and stage.kind in {"sanitize", "benchmark", "profile"}
            and stage.resources is not None
            and stage.resources.mode != "exclusive"
        ):
            raise ContractError(
                f"stage {stage.id!r} kind={stage.kind!r} must use exclusive mode"
            )

    return TaskSpec(
        task_id=task_id,
        description=description,
        workloads=workloads,
        comparison=ComparisonSpec(
            primary_workloads=primary,
            guardrails=guardrails,
            relative_noise_floor=noise,
        ),
        stages=stages,
        raw=raw,
        digest=digest_json(raw),
        source_path=source_path,
    )


def _parse_stage(value: Any, *, index: int, task_dir: Path) -> StageSpec:
    where = f"task.stages[{index}]"
    raw = _expect_object(value, where)
    _strict_keys(
        raw,
        required={"id", "kind", "judge"},
        optional={"execution", "resources", "service_deployment"},
        where=where,
    )
    stage_id = _identifier(raw["id"], f"{where}.id")
    kind = _text(raw["kind"], f"{where}.kind")
    if kind not in STAGE_KINDS:
        raise ContractError(
            f"{where}.kind must be one of {sorted(STAGE_KINDS)}"
        )
    execution = _text(raw.get("execution", "broker"), f"{where}.execution")
    if execution not in EXECUTIONS:
        raise ContractError(
            f"{where}.execution must be one of {sorted(EXECUTIONS)}"
        )

    judge = _expect_object(raw["judge"], f"{where}.judge")
    _strict_keys(
        judge,
        required={"identity", "cwd", "command"},
        optional=set(),
        where=f"{where}.judge",
    )
    identity = _text(judge["identity"], f"{where}.judge.identity")
    cwd_text = _text(judge["cwd"], f"{where}.judge.cwd")
    cwd = Path(cwd_text).expanduser()
    if not cwd.is_absolute():
        cwd = task_dir / cwd
    cwd = cwd.resolve()
    command_value = judge["command"]
    if (
        not isinstance(command_value, list)
        or not command_value
        or not all(isinstance(item, str) and item for item in command_value)
    ):
        raise ContractError(f"{where}.judge.command must be a non-empty string list")

    service_deployment_id = None
    if "service_deployment" in raw:
        if execution != "service":
            raise ContractError(
                f"{where}.service_deployment is allowed only for service execution"
            )
        service_deployment_id = _identifier(
            raw["service_deployment"], f"{where}.service_deployment"
        )

    if execution in {"local", "service"}:
        if "resources" in raw:
            raise ContractError(
                f"{where}.resources must be omitted for {execution} execution"
            )
        return StageSpec(
            id=stage_id,
            kind=kind,
            execution=execution,
            judge_identity=identity,
            cwd=cwd,
            command=tuple(command_value),
            resources=None,
            service_deployment_id=service_deployment_id,
        )

    if "resources" not in raw:
        raise ContractError(f"{where}.resources is required for broker execution")
    resources = _expect_object(raw["resources"], f"{where}.resources")
    _strict_keys(
        resources,
        required={"mode", "gpu_count", "run_timeout_s"},
        optional={"estimate_s", "queue_timeout_s"},
        where=f"{where}.resources",
    )
    mode = _text(resources["mode"], f"{where}.resources.mode")
    if mode not in MODES:
        raise ContractError(f"{where}.resources.mode must be shared or exclusive")
    estimate_raw = resources.get("estimate_s", 600)
    estimate = (
        None
        if estimate_raw is None
        else _positive_number(estimate_raw, f"{where}.resources.estimate_s")
    )
    return StageSpec(
        id=stage_id,
        kind=kind,
        execution=execution,
        judge_identity=identity,
        cwd=cwd,
        command=tuple(command_value),
        resources=ResourceSpec(
            mode=mode,
            gpu_count=_positive_int(
                resources["gpu_count"], f"{where}.resources.gpu_count"
            ),
            estimate_s=estimate,
            queue_timeout_s=_optional_timeout(
                resources.get("queue_timeout_s"),
                f"{where}.resources.queue_timeout_s",
            ),
            run_timeout_s=_positive_number(
                resources["run_timeout_s"], f"{where}.resources.run_timeout_s"
            ),
        ),
        service_deployment_id=None,
    )
