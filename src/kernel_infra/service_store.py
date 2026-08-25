"""Durable immutable deployment history for managed evaluator services."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .service_contracts import ManagedServiceSpec
from .store import RunStore, utc_now

SERVICE_TERMINAL_STATES = frozenset({"stopped", "failed", "interrupted"})


class ServiceStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.deployments_dir = self.root / "services" / "deployments"
        self.deployments_dir.mkdir(parents=True, exist_ok=True)

    def create_deployment(self, spec: ManagedServiceSpec) -> dict[str, Any]:
        deployment_id = f"{spec.service_id}-{uuid.uuid4().hex[:12]}"
        directory = self.deployment_dir(deployment_id)
        directory.mkdir(mode=0o700)
        accepted_at = utc_now()
        RunStore.atomic_json(directory / "spec.json", spec.raw, mode=0o600)
        request = {
            "schema": "kernelinfra.service-request.v1",
            "deployment_id": deployment_id,
            "service_id": spec.service_id,
            "service_sha256": spec.digest,
            "service_source": str(spec.source_path),
            "accepted_at": accepted_at,
            "owner": spec.owner,
            "service_url": spec.service_url,
            "source_root": str(spec.source_root),
            "identity_prefix": spec.identity_prefix,
            "launch": {
                "cwd": str(spec.cwd),
                "command": list(spec.command),
                "env_keys": sorted(spec.env),
            },
            "resources": {
                "mode": "exclusive",
                "gpu_count": spec.resources.gpu_count,
                "estimate_s": spec.resources.estimate_s,
                "queue_timeout_s": spec.resources.queue_timeout_s,
                "run_timeout_s": spec.resources.run_timeout_s,
            },
            "readiness_timeout_s": spec.readiness_timeout_s,
            "idle_grace_s": spec.idle_grace_s,
        }
        RunStore.atomic_json(directory / "request.json", request, mode=0o600)
        state = {
            "schema": "kernelinfra.service-state.v1",
            "deployment_id": deployment_id,
            "service_id": spec.service_id,
            "service_sha256": spec.digest,
            "state": "accepted",
            "broker_job_id": None,
            "gpu_ids": [],
            "service_url": spec.service_url,
            "service_identity": None,
            "admission_receipt": str(directory / "admission.json"),
            "deployment_receipt": str(directory / "deployment.json"),
            "accepted_at": accepted_at,
            "updated_at": accepted_at,
            "ready_at": None,
            "terminal_at": None,
            "reason": None,
            "idle_grace_s": spec.idle_grace_s,
            "idle_since": None,
            "deployment_dir": str(directory),
        }
        RunStore.atomic_json(directory / "state.json", state)
        self.append_event(deployment_id, "accepted", state)
        return state

    def deployment_dir(self, deployment_id: str) -> Path:
        if not deployment_id or "/" in deployment_id or deployment_id in {".", ".."}:
            raise ValueError(f"invalid deployment id: {deployment_id!r}")
        return self.deployments_dir / deployment_id

    def read_state(self, deployment_id: str) -> dict[str, Any]:
        try:
            return json.loads(
                (self.deployment_dir(deployment_id) / "state.json").read_text(
                    encoding="utf-8"
                )
            )
        except FileNotFoundError as exc:
            raise KeyError(f"service deployment not found: {deployment_id}") from exc

    def update_state(
        self, deployment_id: str, event: str, **changes: Any
    ) -> dict[str, Any]:
        state = self.read_state(deployment_id)
        state.update(changes)
        state["updated_at"] = utc_now()
        if state["state"] == "ready" and not state.get("ready_at"):
            state["ready_at"] = state["updated_at"]
        if state["state"] in SERVICE_TERMINAL_STATES and not state.get("terminal_at"):
            state["terminal_at"] = state["updated_at"]
        RunStore.atomic_json(
            self.deployment_dir(deployment_id) / "state.json", state
        )
        self.append_event(deployment_id, event, state)
        return state

    def append_event(
        self, deployment_id: str, event: str, state: dict[str, Any]
    ) -> None:
        value = {
            "schema": "kernelinfra.service-event.v1",
            "at": utc_now(),
            "event": event,
            "deployment_id": deployment_id,
            "service_id": state.get("service_id"),
            "state": state.get("state"),
            "broker_job_id": state.get("broker_job_id"),
            "gpu_ids": state.get("gpu_ids", []),
            "reason": state.get("reason"),
        }
        with (self.deployment_dir(deployment_id) / "events.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")

    def list_states(self, *, service_id: str | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in sorted(self.deployments_dir.glob("*/state.json")):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if service_id is None or state.get("service_id") == service_id:
                result.append(state)
        return sorted(result, key=lambda item: item.get("accepted_at", ""))
