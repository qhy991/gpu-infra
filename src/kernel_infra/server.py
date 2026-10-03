"""Unix-socket JSONL API for non-blocking kernel runs."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import math
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from .candidate import validate_candidate
from . import __version__
from .contracts import ContractError, load_task
from .diagnostics import build_diagnosis
from .frontier import rebuild_frontier
from .runner import RunManager
from .service_store import SERVICE_TERMINAL_STATES
from .services import ServiceManager
from .broker import query_broker
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
        self._locks: list[int] = []

    async def start(self) -> int:
        if self._locks:
            raise RuntimeError("Kernel Infra server already started")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Both identities matter: a different socket must not recover a live
            # state root, and a different state root must not replace a live socket.
            for path in (self.manager.store.root / "daemon.lock",
                         self.socket_path.with_name(self.socket_path.name + ".lock")):
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    raise RuntimeError(f"Kernel Infra owner already active: {path}")
                self._locks.append(fd)
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
            # Reserve the endpoint before recovery; accept clients only once
            # recovery has finished. Failed starts never run shutdown of peers.
            self._server = await asyncio.start_unix_server(
                self._handle, path=self.socket_path, limit=8 * 1024 * 1024,
                start_serving=False,
            )
            self.socket_path.chmod(0o660)
            recovered_runs = await self.manager.recover_interrupted()
            recovered_services = await self.services.recover_interrupted()
            await self._server.start_serving()
            return recovered_runs + recovered_services
        except BaseException:
            await self._release_endpoint()
            raise

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        try:
            await self.manager.close()
            await self.services.close()
        finally:
            await self._release_endpoint()

    async def _release_endpoint(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            self.socket_path.unlink(missing_ok=True)
        # Never unlink lock files: another process may already hold their inode.
        for fd in self._locks:
            os.close(fd)
        self._locks.clear()

    async def _socket_is_live(self) -> bool:
        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
                return False
            raise
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
        if operation == "cancel_checked":
            run_id = str(request["run_id"])
            expected = request.get("expected")
            required = {"task_id", "task_sha256", "candidate_sha256"}
            if not isinstance(expected, dict) or not required.issubset(expected):
                raise ValueError("checked cancel has invalid expected identity")
            if set(expected) - required - {"run_dir"}:
                raise ValueError("checked cancel has unexpected identity fields")
            state = self.manager.store.read_state(run_id)
            for field, value in expected.items():
                if not isinstance(value, str) or state.get(field) != value:
                    raise ValueError(f"checked cancel {field} drift")
            cancelled = await self.manager.cancel(run_id)
            return {
                "ok": True,
                "cancelled": cancelled,
                "run": self.manager.store.read_state(run_id),
            }
        if operation == "frontier":
            task = load_task(Path(request["task"]))
            projection = rebuild_frontier(self.manager.store, task)
            return {"ok": True, "frontier": projection}
        if operation == "service_start":
            state = self.services.start(Path(request["spec"]))
            return {"ok": True, "service": state}
        if operation == "service_preflight":
            result = self.services.preflight(Path(request["spec"]))
            return {"ok": True, "preflight": result}
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
        if operation == "diagnose":
            return {"ok": True, "diagnosis": await self._diagnose(request)}
        raise ValueError(f"unknown operation: {operation!r}")

    async def _diagnose(self, request: dict[str, Any]) -> dict[str, Any]:
        raw_attention_after = request.get("attention_after_s", 300.0)
        if isinstance(raw_attention_after, bool):
            raise ValueError("attention_after_s must be non-negative")
        try:
            attention_after_s = float(raw_attention_after)
        except (TypeError, ValueError) as exc:
            raise ValueError("attention_after_s must be non-negative") from exc
        if not math.isfinite(attention_after_s) or attention_after_s < 0:
            raise ValueError("attention_after_s must be finite and non-negative")

        run_id = request.get("run_id")
        run_states = (
            [self.manager.store.read_state(str(run_id))]
            if run_id
            else [
                state
                for state in self.manager.store.list_states()
                if state.get("state") not in TERMINAL_STATES
            ]
        )
        run_requests: dict[str, dict[str, Any]] = {}
        request_errors: dict[str, str] = {}
        for state in run_states:
            current_run_id = str(state.get("run_id"))
            try:
                run_requests[current_run_id] = self.manager.store.read_request(
                    current_run_id
                )
            except (KeyError, OSError, ValueError) as exc:
                request_errors[current_run_id] = f"{type(exc).__name__}: {exc}"

        broker: dict[str, Any] | None = None
        broker_error: str | None = None
        try:
            broker = await asyncio.to_thread(
                query_broker, self.manager.broker_socket
            )
        except Exception as exc:
            broker_error = f"{type(exc).__name__}: {exc}"

        active_services = [
            state
            for state in self.services.list_statuses()
            if state.get("state") not in SERVICE_TERMINAL_STATES
        ]
        return build_diagnosis(
            observed_at=utc_now(),
            attention_after_s=attention_after_s,
            run_states=run_states,
            run_requests=run_requests,
            request_errors=request_errors,
            broker=broker,
            broker_error=broker_error,
            services=active_services,
        )

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
