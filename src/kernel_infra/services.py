"""Daemon-owned lifecycle for reusable broker-held evaluator services."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .service_attestation import (
    _service_documents,
    atomic_json,
    build_service_receipt,
    load_broker_admission_receipt,
    load_service_receipt,
    verify_service_receipt,
)
from .service_binding import materialize_service_task
from .service_contracts import ManagedServiceSpec, load_service_spec
from .service_store import SERVICE_TERMINAL_STATES, ServiceStore
from .contracts import TaskSpec
from .store import RunStore, TERMINAL_STATES, utc_now

_ACCEPTED = re.compile(r"\[gpu-run\] accepted job (?P<job>\S+)")
_GPU_IDS = re.compile(r"\[gpu-run\] running on physical GPUs (?P<ids>[0-9,]+)")
READY_OR_TERMINAL = frozenset({"ready", *SERVICE_TERMINAL_STATES})


class ServiceManager:
    def __init__(
        self,
        *,
        store: ServiceStore,
        gpu_run: Path,
        broker_socket: Path,
        run_store: RunStore | None = None,
        attest: Callable[..., dict[str, Any]] = build_service_receipt,
        health_check: Callable[[str], Any] = _service_documents,
    ) -> None:
        self.store = store
        self.gpu_run = gpu_run.expanduser().resolve()
        self.broker_socket = broker_socket.expanduser().resolve()
        self.run_store = run_store
        self._attest = attest
        self._health_check = health_check
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._ready_or_done: dict[str, asyncio.Event] = {}
        self._stop_reasons: dict[str, str] = {}
        self._idle_tasks: dict[str, asyncio.Task[None]] = {}

    def active_consumers(self, deployment_id: str) -> list[str]:
        if self.run_store is None:
            return []
        return [
            state["run_id"]
            for state in self.run_store.list_states()
            if state.get("state") not in TERMINAL_STATES
            and deployment_id in state.get("service_deployment_ids", [])
        ]

    def status(self, deployment_id: str) -> dict[str, Any]:
        state = self.store.read_state(deployment_id)
        consumers = self.active_consumers(deployment_id)
        return {
            **state,
            "active_consumers": consumers,
            "active_consumer_count": len(consumers),
        }

    def list_statuses(
        self, *, service_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            self.status(state["deployment_id"])
            for state in self.store.list_states(service_id=service_id)
        ]

    def assert_task_deployments_ready(self, task: TaskSpec) -> None:
        deployment_ids = {
            stage.service_deployment_id
            for stage in task.stages
            if stage.service_deployment_id is not None
        }
        for deployment_id in sorted(deployment_ids):
            state = self.store.read_state(deployment_id)
            if state.get("state") != "ready":
                raise ValueError(
                    f"task service deployment is not ready: {deployment_id} "
                    f"state={state.get('state')}"
                )

    def start(self, spec_path: Path) -> dict[str, Any]:
        spec = load_service_spec(spec_path)
        if not spec.cwd.is_dir():
            raise ValueError(f"service launch cwd is not a directory: {spec.cwd}")
        if not spec.source_root.is_dir():
            raise ValueError(f"service source root is not a directory: {spec.source_root}")
        active = [
            state
            for state in self.store.list_states(service_id=spec.service_id)
            if state.get("state") not in SERVICE_TERMINAL_STATES
        ]
        if active:
            raise ValueError(
                f"service {spec.service_id!r} already has active deployment "
                f"{active[-1]['deployment_id']}"
            )
        if self._endpoint_in_use(spec.service_url):
            raise ValueError(
                f"service endpoint is already in use before launch: {spec.service_url}"
            )
        state = self.store.create_deployment(spec)
        deployment_id = state["deployment_id"]
        self._ready_or_done[deployment_id] = asyncio.Event()
        self._tasks[deployment_id] = asyncio.create_task(
            self._run(deployment_id, spec),
            name=f"kernelinfra-service-{deployment_id}",
        )
        return state

    async def wait(
        self, deployment_id: str, timeout: float | None = None
    ) -> dict[str, Any]:
        state = self.store.read_state(deployment_id)
        if state.get("state") in READY_OR_TERMINAL:
            return self.status(deployment_id)
        event = self._ready_or_done.setdefault(deployment_id, asyncio.Event())
        try:
            if timeout is None:
                await event.wait()
            else:
                await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return self.status(deployment_id)
        return self.status(deployment_id)

    async def stop(
        self, deployment_id: str, *, reason: str = "service stop requested"
    ) -> bool:
        state = self.store.read_state(deployment_id)
        if state.get("state") in SERVICE_TERMINAL_STATES:
            return False
        consumers = self.active_consumers(deployment_id)
        if consumers:
            raise ValueError(
                f"service deployment has active consumers: {','.join(consumers)}"
            )
        self._stop_reasons[deployment_id] = reason
        self.store.update_state(
            deployment_id, "stop_requested", state="stopping", reason=reason
        )
        process = self._processes.get(deployment_id)
        task = self._tasks.get(deployment_id)
        if process is not None and process.returncode is None:
            await self._interrupt_process(process)
        elif task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        terminal = self.store.read_state(deployment_id)
        if terminal.get("state") not in SERVICE_TERMINAL_STATES:
            # A task cancelled before its coroutine first runs never reaches
            # _run's finally block; close that accepted-to-spawn race here.
            self.store.update_state(
                deployment_id,
                "stopped",
                state="stopped",
                reason=reason,
            )
            self._tasks.pop(deployment_id, None)
            self._stop_reasons.pop(deployment_id, None)
            self._ready_or_done.setdefault(deployment_id, asyncio.Event()).set()
        return True

    async def close(self) -> None:
        active = [
            deployment_id
            for deployment_id, task in self._tasks.items()
            if not task.done()
        ]
        await asyncio.gather(
            *(
                self.stop(deployment_id, reason="kernel-infrad shutting down")
                for deployment_id in active
            ),
            return_exceptions=True,
        )

    async def recover_interrupted(self) -> int:
        active = [
            state
            for state in self.store.list_states()
            if state.get("state") not in SERVICE_TERMINAL_STATES
        ]
        for state in active:
            broker_job_id = state.get("broker_job_id")
            if broker_job_id:
                cancelled = await self._cancel_broker_job(str(broker_job_id))
                self.store.update_state(
                    state["deployment_id"],
                    "recovery_broker_reconciled",
                    reason=(
                        f"startup reconciled broker job {broker_job_id}; "
                        f"cancelled={str(cancelled).lower()}"
                    ),
                )
            self.store.update_state(
                state["deployment_id"],
                "interrupted",
                state="interrupted",
                reason="kernel-infrad restarted before service deployment stopped",
            )
            self._ready_or_done.setdefault(
                state["deployment_id"], asyncio.Event()
            ).set()
        return len(active)

    async def bind_task(
        self,
        *,
        deployment_id: str,
        template_path: Path,
        output_path: Path,
        binding_path: Path,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        output = output_path.expanduser().resolve()
        binding_output = binding_path.expanduser().resolve()
        if output == binding_output:
            raise ValueError("task output and binding receipt paths must differ")
        if output.exists():
            raise ValueError(f"refusing to overwrite task output: {output}")
        if binding_output.exists():
            raise ValueError(
                f"refusing to overwrite task binding receipt: {binding_output}"
            )
        state = self.store.read_state(deployment_id)
        if state.get("state") != "ready":
            raise ValueError(
                f"service deployment is not ready: {deployment_id} "
                f"state={state.get('state')}"
            )
        receipt_path = Path(str(state["deployment_receipt"])).resolve()
        receipt = load_service_receipt(receipt_path)
        await asyncio.to_thread(verify_service_receipt, receipt)
        current = self.store.read_state(deployment_id)
        if current.get("state") != "ready" or any(
            current.get(key) != state.get(key)
            for key in (
                "service_sha256",
                "service_identity",
                "deployment_receipt",
                "broker_job_id",
                "gpu_ids",
            )
        ):
            raise RuntimeError("service deployment changed during task binding")
        return materialize_service_task(
            template_path=template_path,
            output_path=output,
            binding_path=binding_output,
            deployment_state=state,
            deployment_receipt=receipt,
        )

    async def _run(self, deployment_id: str, spec: ManagedServiceSpec) -> None:
        directory = self.store.deployment_dir(deployment_id)
        admission_path = directory / "admission.json"
        stdout_path = directory / "stdout.log"
        stderr_path = directory / "stderr.log"
        command = self._gpu_run_command(
            deployment_id=deployment_id,
            spec=spec,
            admission_path=admission_path,
        )
        lease_read_fd, lease_write_fd = os.pipe()
        guard = Path(__file__).with_name("exec_guard.py")
        guarded = [
            sys.executable,
            str(guard),
            "--lease-fd",
            str(lease_read_fd),
            "--",
            *command,
        ]
        process: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[None] | None = None
        stderr_task: asyncio.Task[None] | None = None
        wait_task: asyncio.Task[int] | None = None
        ready_task: asyncio.Task[tuple[str, dict[str, Any]]] | None = None
        failure: str | None = None
        try:
            self.store.update_state(deployment_id, "starting", state="starting")
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *guarded,
                    cwd=str(spec.cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    pass_fds=(lease_read_fd,),
                ),
                name=f"{deployment_id}-spawn",
            )
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # Avoid losing a child created after cancellation but before
                # create_subprocess_exec returns its Process handle.
                process = await spawn
                self._processes[deployment_id] = process
                os.close(lease_read_fd)
                lease_read_fd = -1
                raise
            os.close(lease_read_fd)
            lease_read_fd = -1
            self._processes[deployment_id] = process
            assert process.stdout is not None and process.stderr is not None
            stdout_task = asyncio.create_task(
                self._pump(process.stdout, stdout_path),
                name=f"{deployment_id}-stdout",
            )
            stderr_task = asyncio.create_task(
                self._pump_stderr(deployment_id, process.stderr, stderr_path),
                name=f"{deployment_id}-stderr",
            )
            wait_task = asyncio.create_task(
                process.wait(), name=f"{deployment_id}-wait"
            )
            ready_task = asyncio.create_task(
                self._attest_when_ready(
                    deployment_id=deployment_id,
                    spec=spec,
                    admission_path=admission_path,
                    process_wait=wait_task,
                ),
                name=f"{deployment_id}-ready",
            )
            done, _pending = await asyncio.wait(
                {wait_task, ready_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if ready_task in done:
                service_identity, receipt = ready_task.result()
                atomic_json(directory / "deployment.json", receipt)
                self.store.update_state(
                    deployment_id,
                    "ready",
                    state="ready",
                    service_identity=service_identity,
                    broker_job_id=receipt["broker_job_id"],
                    gpu_ids=receipt["broker_gpu_ids"],
                    reason=None,
                )
                if spec.idle_grace_s is not None:
                    self._idle_tasks[deployment_id] = asyncio.create_task(
                        self._idle_watch(
                            deployment_id=deployment_id,
                            grace_s=spec.idle_grace_s,
                        ),
                        name=f"{deployment_id}-idle-watch",
                    )
                self._ready_or_done.setdefault(deployment_id, asyncio.Event()).set()
                await wait_task
            else:
                ready_task.cancel()
                await asyncio.gather(ready_task, return_exceptions=True)
                failure = (
                    f"service gpu-run exited before readiness: {wait_task.result()}"
                )
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                await self._interrupt_process(process)
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            if process is not None and process.returncode is None:
                await self._interrupt_process(process)
        finally:
            idle_task = self._idle_tasks.pop(deployment_id, None)
            if idle_task is not None and idle_task is not asyncio.current_task():
                idle_task.cancel()
                await asyncio.gather(idle_task, return_exceptions=True)
            if ready_task is not None and not ready_task.done():
                ready_task.cancel()
            await asyncio.gather(
                *(task for task in (ready_task, stdout_task, stderr_task) if task),
                return_exceptions=True,
            )
            try:
                os.close(lease_write_fd)
            except OSError:
                pass
            if lease_read_fd >= 0:
                try:
                    os.close(lease_read_fd)
                except OSError:
                    pass
            self._processes.pop(deployment_id, None)
            stop_reason = self._stop_reasons.get(deployment_id)
            if stop_reason is not None:
                self.store.update_state(
                    deployment_id,
                    "stopped",
                    state="stopped",
                    reason=stop_reason,
                )
            else:
                if failure is None:
                    exit_code = process.returncode if process is not None else None
                    failure = f"service gpu-run exited unexpectedly: {exit_code}"
                self.store.update_state(
                    deployment_id,
                    "failed",
                    state="failed",
                    reason=failure,
                )
            self._ready_or_done.setdefault(deployment_id, asyncio.Event()).set()
            self._tasks.pop(deployment_id, None)
            self._stop_reasons.pop(deployment_id, None)

    async def _idle_watch(self, *, deployment_id: str, grace_s: float) -> None:
        idle_started_mono: float | None = None
        while True:
            state = self.store.read_state(deployment_id)
            if state.get("state") != "ready":
                return
            consumers = self.active_consumers(deployment_id)
            if consumers:
                idle_started_mono = None
                if state.get("idle_since") is not None:
                    self.store.update_state(
                        deployment_id,
                        "idle_cleared",
                        idle_since=None,
                    )
            else:
                if idle_started_mono is None:
                    idle_started_mono = time.monotonic()
                    if state.get("idle_since") is None:
                        self.store.update_state(
                            deployment_id,
                            "idle_started",
                            idle_since=utc_now(),
                        )
                elif time.monotonic() - idle_started_mono >= grace_s:
                    # No await occurs between the final ledger projection and
                    # the stopping transition, so submit and auto-stop are
                    # serialized by the daemon event loop.
                    current = self.store.read_state(deployment_id)
                    if (
                        current.get("state") == "ready"
                        and not self.active_consumers(deployment_id)
                    ):
                        reason = f"idle grace elapsed with zero consumers: {grace_s:g}s"
                        self._stop_reasons[deployment_id] = reason
                        self.store.update_state(
                            deployment_id,
                            "idle_stop_requested",
                            state="stopping",
                            reason=reason,
                        )
                        process = self._processes.get(deployment_id)
                        if process is not None and process.returncode is None:
                            await self._interrupt_process(process)
                    return
            await asyncio.sleep(min(0.25, max(0.05, grace_s / 4)))

    async def _attest_when_ready(
        self,
        *,
        deployment_id: str,
        spec: ManagedServiceSpec,
        admission_path: Path,
        process_wait: asyncio.Task[int],
    ) -> tuple[str, dict[str, Any]]:
        deadline = time.monotonic() + spec.readiness_timeout_s
        while not admission_path.is_file():
            if process_wait.done():
                raise RuntimeError("gpu-run exited before writing admission receipt")
            if time.monotonic() >= deadline:
                raise TimeoutError("service admission receipt readiness timed out")
            await asyncio.sleep(0.1)
        admission = load_broker_admission_receipt(admission_path)
        self.store.update_state(
            deployment_id,
            "broker_started",
            broker_job_id=admission["job_id"],
            gpu_ids=admission["gpu_ids"],
        )
        last_health_error = "service did not become healthy"
        while True:
            if process_wait.done():
                raise RuntimeError("gpu-run exited before service became healthy")
            try:
                await asyncio.to_thread(self._health_check, spec.service_url)
                break
            except Exception as exc:
                last_health_error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"service health readiness timed out: {last_health_error}"
                )
            await asyncio.sleep(0.25)
        identity = (
            f"{spec.identity_prefix}"
            f"+admission@sha256:{admission['launch_spec_sha256']}"
            f"+executable@sha256:{admission['executable_sha256']}"
        )
        receipt = await asyncio.to_thread(
            self._attest,
            broker_socket=self.broker_socket,
            broker_job_id=admission["job_id"],
            broker_admission_receipt=admission_path,
            service_url=spec.service_url,
            service_identity=identity,
            source_root=spec.source_root,
        )
        return identity, receipt

    def _gpu_run_command(
        self,
        *,
        deployment_id: str,
        spec: ManagedServiceSpec,
        admission_path: Path,
    ) -> list[str]:
        resources = spec.resources
        command = [
            str(self.gpu_run),
            "--socket",
            str(self.broker_socket),
            "--label",
            f"ki-service:{spec.service_id}:{deployment_id}"[:160],
            "--mode",
            "exclusive",
            "--gpu-count",
            str(resources.gpu_count),
            "--owner",
            spec.owner,
            "--estimate",
            "unknown" if resources.estimate_s is None else str(resources.estimate_s),
            "--run-timeout",
            str(resources.run_timeout_s),
            "--receipt-out",
            str(admission_path),
        ]
        if resources.queue_timeout_s is not None:
            command.extend(["--queue-timeout", str(resources.queue_timeout_s)])
        for key, value in sorted(spec.env.items()):
            command.extend(["--env", f"{key}={value}"])
        command.extend(["--", *spec.command])
        return command

    async def _pump(
        self, reader: asyncio.StreamReader, path: Path
    ) -> None:
        with path.open("ab") as handle:
            while chunk := await reader.read(65536):
                handle.write(chunk)
                handle.flush()

    async def _pump_stderr(
        self, deployment_id: str, reader: asyncio.StreamReader, path: Path
    ) -> None:
        with path.open("ab") as handle:
            while True:
                line = await reader.readline()
                if not line:
                    return
                handle.write(line)
                handle.flush()
                text = line.decode("utf-8", "replace").strip()
                accepted = _ACCEPTED.search(text)
                if accepted:
                    self.store.update_state(
                        deployment_id,
                        "broker_accepted",
                        broker_job_id=accepted.group("job"),
                    )
                    continue
                gpu_ids = _GPU_IDS.search(text)
                if gpu_ids:
                    self.store.update_state(
                        deployment_id,
                        "broker_running",
                        gpu_ids=[int(value) for value in gpu_ids.group("ids").split(",")],
                    )

    async def _interrupt_process(
        self, process: asyncio.subprocess.Process
    ) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=15)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    return
                await process.wait()

    async def _cancel_broker_job(self, job_id: str) -> bool:
        reader: asyncio.StreamReader
        writer: asyncio.StreamWriter
        try:
            reader, writer = await asyncio.open_unix_connection(self.broker_socket)
            writer.write(
                (json.dumps({"op": "cancel", "job_id": job_id}) + "\n").encode()
            )
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), timeout=10))
        except (OSError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"cannot reconcile broker job {job_id} at {self.broker_socket}: {exc}"
            ) from exc
        finally:
            if "writer" in locals():
                writer.close()
                await writer.wait_closed()
        return bool(response.get("ok"))

    @staticmethod
    def _endpoint_in_use(service_url: str) -> bool:
        parsed = urllib.parse.urlparse(service_url)
        assert parsed.hostname is not None and parsed.port is not None
        try:
            with socket.create_connection(
                (parsed.hostname, parsed.port), timeout=0.2
            ):
                return True
        except OSError:
            return False
