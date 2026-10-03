"""Read-only correlation of run progress with live broker GPU state."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
from typing import Any

from .store import TERMINAL_STATES

DIAGNOSIS_SCHEMA = "kernelinfra.diagnosis.v1"
ATTENTION_DIAGNOSES = frozenset({"long_wait", "suspected_stall"})


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _seconds_since(value: Any, now: datetime) -> float | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return round(max(0.0, (now - parsed).total_seconds()), 3)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _current_stage(
    state: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any] | None:
    index = state.get("stage_index")
    stages = request.get("stages")
    if isinstance(index, bool) or not isinstance(index, int) or not isinstance(stages, list):
        return None
    if index < 0 or index >= len(stages) or not isinstance(stages[index], dict):
        return None
    return stages[index]


def _job_view(job: dict[str, Any] | None, location: str) -> dict[str, Any] | None:
    if job is None:
        return None
    fields = (
        "job_id",
        "state",
        "label",
        "mode",
        "gpu_count",
        "gpu_ids",
        "submitted_at",
        "started_at",
        "wait_seconds",
        "run_seconds",
        "position",
        "eta_seconds",
        "reason",
    )
    return {"location": location, **{key: job.get(key) for key in fields}}


def _classify_run(
    *,
    state: dict[str, Any],
    request: dict[str, Any] | None,
    request_error: str | None,
    running_jobs: dict[str, dict[str, Any]],
    queued_jobs: dict[str, dict[str, Any]],
    recent_jobs: dict[str, dict[str, Any]],
    broker_unknown: bool,
    now: datetime,
    attention_after_s: float,
) -> dict[str, Any]:
    no_progress_s = _seconds_since(state.get("updated_at"), now)
    result = {
        "run_id": state.get("run_id"),
        "task_id": state.get("task_id"),
        "state": state.get("state"),
        "stage_id": state.get("stage_id"),
        "stage_kind": state.get("stage_kind"),
        "stage_execution": None,
        "broker_job_id": state.get("broker_job_id"),
        "gpu_ids": state.get("gpu_ids", []),
        "accepted_at": state.get("accepted_at"),
        "updated_at": state.get("updated_at"),
        "no_progress_seconds": no_progress_s,
        "diagnosis": "progressing",
        "reason": "run has recent lifecycle progress",
        "broker_observation": None,
    }
    if state.get("state") in TERMINAL_STATES:
        result.update(
            diagnosis="terminal",
            reason=state.get("reason") or "run is terminal",
        )
        return result
    if no_progress_s is None:
        result.update(
            diagnosis="unknown",
            reason="run updated_at is missing or invalid",
        )
        return result
    if request_error is not None or request is None:
        result.update(
            diagnosis="unknown",
            reason=request_error or "run request is unavailable",
        )
        return result

    stage = _current_stage(state, request)
    execution = stage.get("execution") if stage is not None else None
    result["stage_execution"] = execution
    broker_job_id = state.get("broker_job_id")

    if execution in {"local", "service"}:
        if state.get("state") == "waiting_local":
            if no_progress_s >= attention_after_s:
                result.update(
                    diagnosis="long_wait",
                    reason=(
                        f"{execution} stage has waited {no_progress_s:g}s without "
                        "a lifecycle transition"
                    ),
                )
            else:
                result.update(
                    diagnosis="waiting",
                    reason=f"{execution} stage is waiting for local capacity",
                )
        else:
            result.update(
                diagnosis="running",
                reason=f"{execution} stage is active; lifecycle age alone cannot prove a stall",
            )
        return result

    if broker_unknown and (execution == "broker" or broker_job_id):
        result.update(
            diagnosis="unknown",
            reason="live broker observation is unavailable",
        )
        return result

    if not broker_job_id:
        if no_progress_s >= attention_after_s:
            result.update(
                diagnosis="suspected_stall",
                reason=(
                    "run has not acquired a broker job and has no lifecycle "
                    f"progress for {no_progress_s:g}s"
                ),
            )
        return result

    job_id = str(broker_job_id)
    job = running_jobs.get(job_id)
    location = "running"
    if job is None:
        job = queued_jobs.get(job_id)
        location = "queue"
    if job is None:
        job = recent_jobs.get(job_id)
        location = "recent"
    result["broker_observation"] = _job_view(job, location)

    resources = stage.get("resources") if stage is not None else None
    if not isinstance(resources, dict):
        resources = {}

    if location == "queue" and job is not None:
        wait_s = _number(job.get("wait_seconds"))
        if wait_s is None:
            result.update(
                diagnosis="unknown",
                reason="broker queue observation has no numeric wait_seconds",
            )
            return result
        queue_timeout_s = _number(resources.get("queue_timeout_s"))
        if queue_timeout_s is not None and wait_s > queue_timeout_s + 5.0:
            result.update(
                diagnosis="suspected_stall",
                reason=(
                    f"broker job remains queued at {wait_s:g}s beyond declared "
                    f"queue timeout {queue_timeout_s:g}s"
                ),
            )
        elif wait_s >= attention_after_s:
            result.update(
                diagnosis="long_wait",
                reason=(
                    f"broker job has waited {wait_s:g}s at queue position "
                    f"{job.get('position', 'unknown')}"
                ),
            )
        else:
            result.update(
                diagnosis="waiting",
                reason=(
                    f"broker job is queued at position "
                    f"{job.get('position', 'unknown')}"
                ),
            )
        return result

    if location == "running" and job is not None:
        run_s = _number(job.get("run_seconds"))
        observed_gpu_ids = job.get("gpu_ids")
        if run_s is None or not isinstance(observed_gpu_ids, list):
            result.update(
                diagnosis="unknown",
                reason="broker running observation lacks duration or GPU assignment",
            )
            return result
        run_timeout_s = _number(resources.get("run_timeout_s"))
        state_gpu_ids = state.get("gpu_ids", [])
        if run_timeout_s is not None and run_s > run_timeout_s + 5.0:
            result.update(
                diagnosis="suspected_stall",
                reason=(
                    f"broker job remains running at {run_s:g}s beyond declared "
                    f"run timeout {run_timeout_s:g}s"
                ),
            )
        elif state_gpu_ids and state_gpu_ids != observed_gpu_ids:
            result.update(
                diagnosis="suspected_stall",
                reason="run GPU assignment disagrees with the live broker job",
            )
        elif state.get("state") != "running" and no_progress_s >= attention_after_s:
            result.update(
                diagnosis="suspected_stall",
                reason=(
                    "broker job is running but run lifecycle has not caught up for "
                    f"{no_progress_s:g}s"
                ),
            )
        elif state.get("state") != "running":
            result.update(
                diagnosis="progressing",
                reason="broker job has started and run lifecycle is catching up",
            )
        else:
            result.update(
                diagnosis="running",
                reason=f"broker job is running on GPUs {observed_gpu_ids}",
            )
        return result

    if location == "recent" and job is not None:
        if no_progress_s >= attention_after_s:
            result.update(
                diagnosis="suspected_stall",
                reason=(
                    f"broker job is already {job.get('state', 'terminal')} but the "
                    f"run has not advanced for {no_progress_s:g}s"
                ),
            )
        else:
            result.update(
                diagnosis="progressing",
                reason="broker job is terminal and run aggregation is catching up",
            )
        return result

    if no_progress_s >= attention_after_s:
        result.update(
            diagnosis="suspected_stall",
            reason=(
                "run references a broker job absent from the live broker for "
                f"{no_progress_s:g}s"
            ),
        )
    else:
        result.update(
            diagnosis="progressing",
            reason="broker job transition is not yet reflected in the snapshot",
        )
    return result


def build_diagnosis(
    *,
    observed_at: str,
    attention_after_s: float,
    run_states: list[dict[str, Any]],
    run_requests: dict[str, dict[str, Any]],
    request_errors: dict[str, str],
    broker: dict[str, Any] | None,
    broker_error: str | None,
    services: list[dict[str, Any]],
) -> dict[str, Any]:
    now = _parse_timestamp(observed_at)
    if now is None:
        raise ValueError("observed_at must be a timezone-aware timestamp")
    if _number(attention_after_s) is None:
        raise ValueError("attention_after_s must be finite and non-negative")

    snapshot = broker or {}
    probe_error = snapshot.get("probe_error")
    raw_gpus = snapshot.get("gpus")
    raw_running = snapshot.get("running")
    raw_queue = snapshot.get("queue")
    raw_recent = snapshot.get("recent", [])
    payload_lists_valid = all(
        isinstance(value, list)
        for value in (raw_gpus, raw_running, raw_queue, raw_recent)
    )
    gpus = [gpu for gpu in raw_gpus if isinstance(gpu, dict)] if isinstance(raw_gpus, list) else []
    running = (
        [job for job in raw_running if isinstance(job, dict)]
        if isinstance(raw_running, list)
        else []
    )
    queue = (
        [job for job in raw_queue if isinstance(job, dict)]
        if isinstance(raw_queue, list)
        else []
    )
    recent = (
        [job for job in raw_recent if isinstance(job, dict)]
        if isinstance(raw_recent, list)
        else []
    )
    payload_items_valid = (
        len(gpus) == len(raw_gpus)
        and len(running) == len(raw_running)
        and len(queue) == len(raw_queue)
        and len(recent) == len(raw_recent)
    ) if payload_lists_valid else False
    broker_unknown = (
        broker is None
        or broker_error is not None
        or bool(probe_error)
        or not snapshot.get("broker_version")
        or not snapshot.get("instance_id")
        or not gpus
        or not payload_items_valid
    )
    if any(gpu.get("state") not in ("idle", "shared", "exclusive", "foreign", "locked")
           or isinstance(gpu.get("gpu_id"), bool)
           or not isinstance(gpu.get("gpu_id"), int) for gpu in gpus):
        broker_unknown = True
    if any(not isinstance(job.get("job_id"), str) or not job["job_id"]
           or _number(job.get(field)) is None
           for jobs, field in ((running, "run_seconds"), (queue, "wait_seconds"))
           for job in jobs):
        broker_unknown = True
    effective_broker_error = broker_error
    if broker is not None and broker_unknown and not probe_error and broker_error is None:
        effective_broker_error = "broker status is incomplete or malformed"

    running_jobs = {
        str(job["job_id"]): job
        for job in running
        if isinstance(job, dict) and job.get("job_id")
    }
    queued_jobs = {
        str(job["job_id"]): job
        for job in queue
        if isinstance(job, dict) and job.get("job_id")
    }
    recent_jobs = {
        str(job["job_id"]): job
        for job in recent
        if isinstance(job, dict) and job.get("job_id")
    }
    runs = [
        _classify_run(
            state=state,
            request=run_requests.get(str(state.get("run_id"))),
            request_error=request_errors.get(str(state.get("run_id"))),
            running_jobs=running_jobs,
            queued_jobs=queued_jobs,
            recent_jobs=recent_jobs,
            broker_unknown=broker_unknown,
            now=now,
            attention_after_s=attention_after_s,
        )
        for state in run_states
    ]
    run_counts = Counter(run["diagnosis"] for run in runs)
    gpu_counts = Counter(
        str(gpu.get("state", "unknown")) for gpu in gpus if isinstance(gpu, dict)
    )
    if broker_unknown or run_counts["unknown"]:
        verdict = "unknown"
    elif (any(run_counts[name] for name in ATTENTION_DIAGNOSES)
          or any((_number(job.get("wait_seconds")) or 0) >= attention_after_s
                 for job in queue)):
        verdict = "attention"
    else:
        verdict = "ok"

    return {
        "schema": DIAGNOSIS_SCHEMA,
        "observed_at": observed_at,
        "attention_after_seconds": attention_after_s,
        "verdict": verdict,
        "summary": {
            "gpu_states": dict(sorted(gpu_counts.items())),
            "running_broker_jobs": len(running),
            "queued_broker_jobs": len(queue),
            "run_diagnoses": dict(sorted(run_counts.items())),
            "active_services": len(services),
        },
        "broker": {
            "observation": "unknown" if broker_unknown else "ok",
            "error": effective_broker_error,
            "version": snapshot.get("version"),
            "broker_version": snapshot.get("broker_version"),
            "instance_id": snapshot.get("instance_id"),
            "updated_at": snapshot.get("updated_at"),
            "gpu_observed_at": snapshot.get("gpu_observed_at"),
            "probe_error": probe_error,
            "shared_capacity": snapshot.get("shared_capacity"),
            "gpus": gpus,
            "running": [_job_view(job, "running") for job in running],
            "queue": [_job_view(job, "queue") for job in queue],
        },
        "runs": runs,
        "services": [
            {
                key: state.get(key)
                for key in (
                    "deployment_id",
                    "service_id",
                    "state",
                    "broker_job_id",
                    "gpu_ids",
                    "active_consumer_count",
                    "updated_at",
                    "reason",
                )
            }
            for state in services
        ],
    }
