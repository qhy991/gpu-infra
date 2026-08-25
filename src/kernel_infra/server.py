"""Unix-socket JSONL API for non-blocking kernel runs."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from .candidate import validate_candidate
from . import __version__
from .contracts import ContractError, load_task
from .frontier import rebuild_frontier
from .runner import RunManager
from .services import ServiceManager
from .service_attestation import query_broker
from .store import TERMINAL_STATES, utc_now


def _daemon_instance_id() -> str:
    start = "unknown"
    try:
        start = Path("/proc/self/stat").read_text().split()[21]
    except (OSError, IndexError):
        pass
    return f"{os.uname().nodename}-pid{os.getpid()}-start{start}"


class KernelInfraServer:
    def __init__(
        self,
        manager: RunManager,
        services: ServiceManager,
        socket_path: Path,
    ) -> None:
        self.manager = manager
        self.services = services
        self.socket_path = socket_path.expanduser().resolve()
        self.instance_id = _daemon_instance_id()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        recovered_runs = await self.manager.recover_interrupted()
        recovered_services = await self.services.recover_interrupted()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            if not stat.S_ISSOCK(self.socket_path.stat().st_mode):
                raise RuntimeError(
                    f"refusing to replace non-socket path: {self.socket_path}"
                )
            if await self._socket_is_live():
                raise RuntimeError(
                    f"Kernel Infra socket already active: {self.socket_path}"
                )
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle, path=self.socket_path, limit=8 * 1024 * 1024
        )
        self.socket_path.chmod(0o660)
        return recovered_runs + recovered_services

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        await self.manager.close()
        await self.services.close()
        try:
            if stat.S_ISSOCK(self.socket_path.stat().st_mode):
                self.socket_path.unlink()
        except FileNotFoundError:
            pass

    async def _socket_is_live(self) -> bool:
        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
        except OSError:
            return False
        writer.close()
        await writer.wait_closed()
        del reader
        return True

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            request = json.loads(line)
            response = await self._dispatch(request)
        except (
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            ContractError,
        ) as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
        try:
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    async def _dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        operation = request.get("op")
        if operation == "submit":
            task = load_task(Path(request["task"]))
            self.services.assert_task_deployments_ready(task)
            state = self.manager.submit(
                task_path=Path(request["task"]),
                candidate=Path(request["candidate"]),
                label=request.get("label"),
                task_spec=task,
            )
            return {"ok": True, "run": state}
        if operation == "submit_many":
            candidates = request.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                raise ValueError("candidates must be a non-empty list")
            # Fail before accepting any run when a batch contains an invalid
            # candidate. Snapshotting repeats this check to close the mutation
            # window for cooperating clients.
            task = load_task(Path(request["task"]))
            self.services.assert_task_deployments_ready(task)
            for candidate in candidates:
                validate_candidate(Path(candidate))
            prefix = str(request.get("label_prefix") or "").strip()
            runs = []
            for index, candidate in enumerate(candidates):
                path = Path(candidate)
                label = f"{prefix}{index:03d}" if prefix else path.name
                runs.append(
                    self.manager.submit(
                        task_path=Path(request["task"]),
                        candidate=path,
                        label=label,
                        task_spec=task,
                    )
                )
            return {"ok": True, "runs": runs}
        if operation == "status":
            run_id = request.get("run_id")
            states = (
                [self.manager.store.read_state(str(run_id))]
                if run_id
                else self.manager.store.list_states(task_id=request.get("task_id"))
            )
            return {"ok": True, "runs": states}
        if operation == "wait":
            timeout = request.get("timeout")
            if timeout is not None:
                timeout = float(timeout)
                if timeout < 0:
                    raise ValueError("timeout must be non-negative")
            state = await self.manager.wait(str(request["run_id"]), timeout)
            return {"ok": True, "run": state}
        if operation == "cancel":
            cancelled = await self.manager.cancel(str(request["run_id"]))
            return {"ok": True, "cancelled": cancelled}
        if operation == "frontier":
            task = load_task(Path(request["task"]))
            projection = rebuild_frontier(self.manager.store, task)
            return {"ok": True, "frontier": projection}
        if operation == "service_start":
            state = self.services.start(Path(request["spec"]))
            return {"ok": True, "service": state}
        if operation == "service_status":
            deployment_id = request.get("deployment_id")
            states = (
                [self.services.status(str(deployment_id))]
                if deployment_id
                else self.services.list_statuses(
                    service_id=request.get("service_id")
                )
            )
            return {"ok": True, "services": states}
        if operation == "service_wait":
            timeout = request.get("timeout")
            if timeout is not None:
                timeout = float(timeout)
                if timeout < 0:
                    raise ValueError("timeout must be non-negative")
            state = await self.services.wait(
                str(request["deployment_id"]), timeout
            )
            return {"ok": True, "service": state}
        if operation == "service_stop":
            stopped = await self.services.stop(str(request["deployment_id"]))
            return {"ok": True, "stopped": stopped}
        if operation == "service_bind_task":
            task, binding = await self.services.bind_task(
                deployment_id=str(request["deployment_id"]),
                template_path=Path(request["template"]),
                output_path=Path(request["output"]),
                binding_path=Path(request["binding_output"]),
            )
            return {"ok": True, "task": task, "binding": binding}
        if operation == "node_status":
            return {"ok": True, "node": await self._node_status()}
        raise ValueError(f"unknown operation: {operation!r}")

    async def _node_status(self) -> dict[str, Any]:
        broker = await asyncio.to_thread(query_broker, self.manager.broker_socket)
        disk = shutil.disk_usage(self.manager.store.root)
        active_runs = [
            state
            for state in self.manager.store.list_states()
            if state.get("state") not in TERMINAL_STATES
        ]
        service_states = self.services.list_statuses()
        return {
            "schema": "kernelinfra.node-status.v1",
            "observed_at": utc_now(),
            "kernelinfra_version": __version__,
            "daemon_instance_id": self.instance_id,
            "state_root": str(self.manager.store.root),
            "disk": {
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
            },
            "active_runs": [
                {
                    "run_id": state["run_id"],
                    "task_id": state["task_id"],
                    "state": state["state"],
                    "service_deployment_ids": state.get(
                        "service_deployment_ids", []
                    ),
                }
                for state in active_runs
            ],
            "services": [
                {
                    "deployment_id": state["deployment_id"],
                    "service_id": state["service_id"],
                    "state": state["state"],
                    "active_consumer_count": state["active_consumer_count"],
                }
                for state in service_states
            ],
            "ready_deployments": [
                state["deployment_id"]
                for state in service_states
                if state["state"] == "ready"
            ],
            "broker": {
                "version": broker.get("version"),
                "broker_version": broker.get("broker_version"),
                "instance_id": broker.get("instance_id"),
                "probe_error": broker.get("probe_error"),
                "shared_capacity": broker.get("shared_capacity"),
                "gpus": broker.get("gpus", []),
                "running": broker.get("running", []),
                "queue": broker.get("queue", []),
            },
        }
