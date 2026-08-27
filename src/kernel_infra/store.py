"""Durable run artifacts with atomic current-state projections."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .candidate import snapshot_candidate
from .contracts import TaskSpec

TERMINAL_STATES = frozenset(
    {"completed", "rejected", "infra_error", "cancelled", "interrupted"}
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.runs_dir = self.root / "runs"
        self.frontiers_dir = self.root / "frontiers"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.frontiers_dir.mkdir(parents=True, exist_ok=True)

    def create_run(
        self,
        *,
        task: TaskSpec,
        candidate: Path,
        label: str,
    ) -> dict[str, Any]:
        run_id = f"{task.task_id}-{uuid.uuid4().hex[:12]}"
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(mode=0o755)
        try:
            candidate_digest = snapshot_candidate(candidate, run_dir / "candidate")
        except Exception:
            shutil.rmtree(run_dir)
            raise
        self.atomic_json(run_dir / "task.json", task.raw, mode=0o644)
        request = {
            "schema": "kernelinfra.request.v1",
            "run_id": run_id,
            "task_id": task.task_id,
            "task_sha256": task.digest,
            "candidate_sha256": candidate_digest,
            "candidate_source": str(candidate.expanduser().resolve()),
            "task_source": str(task.source_path),
            "label": label,
            "accepted_at": utc_now(),
            "stages": [
                {
                    "id": stage.id,
                    "kind": stage.kind,
                    "execution": stage.execution,
                    "judge_identity": stage.judge_identity,
                    "resolved_cwd": str(stage.cwd),
                    "command": list(stage.command),
                    "service_deployment": stage.service_deployment_id,
                    "resources": (
                        {
                            "mode": stage.resources.mode,
                            "gpu_count": stage.resources.gpu_count,
                            "estimate_s": stage.resources.estimate_s,
                            "queue_timeout_s": stage.resources.queue_timeout_s,
                            "run_timeout_s": stage.resources.run_timeout_s,
                        }
                        if stage.resources is not None
                        else None
                    ),
                }
                for stage in task.stages
            ],
        }
        self.atomic_json(run_dir / "request.json", request, mode=0o644)
        state = {
            "schema": "kernelinfra.state.v1",
            "run_id": run_id,
            "task_id": task.task_id,
            "task_sha256": task.digest,
            "candidate_sha256": candidate_digest,
            "label": label,
            "state": "accepted",
            "stage_id": None,
            "stage_kind": None,
            "stage_index": None,
            "broker_job_id": None,
            "gpu_ids": [],
            "service_deployment_ids": sorted(
                {
                    stage.service_deployment_id
                    for stage in task.stages
                    if stage.service_deployment_id is not None
                }
            ),
            "accepted_at": request["accepted_at"],
            "updated_at": request["accepted_at"],
            "terminal_at": None,
            "reason": None,
            "run_dir": str(run_dir),
        }
        self.atomic_json(run_dir / "state.json", state)
        self.append_event(run_id, "accepted", state)
        return state

    def run_dir(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or run_id in {".", ".."}:
            raise ValueError(f"invalid run id: {run_id!r}")
        return self.runs_dir / run_id

    def read_state(self, run_id: str) -> dict[str, Any]:
        path = self.run_dir(run_id) / "state.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError(f"run not found: {run_id}") from exc

    def update_state(
        self,
        run_id: str,
        event: str,
        **changes: Any,
    ) -> dict[str, Any]:
        state = self.read_state(run_id)
        state.update(changes)
        state["updated_at"] = utc_now()
        if state["state"] in TERMINAL_STATES and not state.get("terminal_at"):
            state["terminal_at"] = state["updated_at"]
        self.atomic_json(self.run_dir(run_id) / "state.json", state)
        self.append_event(run_id, event, state)
        return state

    def append_event(
        self, run_id: str, event: str, state: dict[str, Any]
    ) -> None:
        value = {
            "schema": "kernelinfra.event.v1",
            "at": utc_now(),
            "event": event,
            "run_id": run_id,
            "state": state.get("state"),
            "stage_id": state.get("stage_id"),
            "broker_job_id": state.get("broker_job_id"),
            "gpu_ids": state.get("gpu_ids", []),
            "reason": state.get("reason"),
        }
        with (self.run_dir(run_id) / "events.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")

    def list_states(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in sorted(self.runs_dir.glob("*/state.json")):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if task_id is None or state.get("task_id") == task_id:
                result.append(state)
        return sorted(result, key=lambda item: item.get("accepted_at", ""))

    def recover_interrupted(self) -> int:
        changed = 0
        for state in self.list_states():
            if state.get("state") in TERMINAL_STATES:
                continue
            self.update_state(
                state["run_id"],
                "interrupted",
                state="interrupted",
                reason="kernel-infrad restarted before the run became terminal",
            )
            changed += 1
        return changed

    @staticmethod
    def atomic_json(path: Path, value: Any, *, mode: int = 0o644) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
