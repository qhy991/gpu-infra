"""Background staged execution through agent-gpu-broker."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from pathlib import Path
from typing import Any

from .contracts import ContractError, StageSpec, TaskSpec, load_task
from .frontier import rebuild_frontier
from .results import aggregate_run_result, load_stage_result
from .store import RunStore, TERMINAL_STATES, utc_now

_ACCEPTED = re.compile(r"\[gpu-run\] accepted job (?P<job>\S+)")
_GPU_IDS = re.compile(r"\[gpu-run\] running on physical GPUs (?P<ids>[0-9,]+)")


class RunManager:
    def __init__(
        self,
        *,
        store: RunStore,
        gpu_run: Path,
        broker_socket: Path,
    ) -> None:
        self.store = store
        self.gpu_run = gpu_run.expanduser().resolve()
        self.broker_socket = broker_socket.expanduser().resolve()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._stop_reasons: dict[str, str] = {}

    def recover_interrupted(self) -> int:
        return self.store.recover_interrupted()

    def submit(
        self, *, task_path: Path, candidate: Path, label: str | None = None
    ) -> dict[str, Any]:
        task = load_task(task_path)
        effective_label = (label or candidate.name or task.task_id).strip()
        if not effective_label:
            raise ValueError("label must not be empty")
        state = self.store.create_run(
            task=task, candidate=candidate, label=effective_label
        )
        run_id = state["run_id"]
        self._done[run_id] = asyncio.Event()
        self._tasks[run_id] = asyncio.create_task(
            self._run(run_id, task), name=f"kernelinfra-{run_id}"
        )
        return state

    async def wait(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        state = self.store.read_state(run_id)
        if state["state"] in TERMINAL_STATES:
            return state
        event = self._done.get(run_id)
        if event is None:
            return state
        try:
            if timeout is None:
                await event.wait()
            else:
                await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self.store.read_state(run_id)

    async def cancel(self, run_id: str) -> bool:
        state = self.store.read_state(run_id)
        if state["state"] in TERMINAL_STATES:
            return False
        task = self._tasks.get(run_id)
        if task is None:
            return False
        self._stop_reasons[run_id] = "cancelled"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def close(self) -> None:
        for run_id, task in list(self._tasks.items()):
            if task.done():
                continue
            self._stop_reasons[run_id] = "interrupted"
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _run(self, run_id: str, task: TaskSpec) -> None:
        run_dir = self.store.run_dir(run_id)
        request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
        stage_results: list[tuple[str, str, dict[str, Any]]] = []
        terminal_reason: str | None = None
        terminal_state = "infra_error"
        try:
            for index, stage in enumerate(task.stages):
                result, exit_code, error = await self._run_stage(
                    run_id=run_id,
                    task=task,
                    stage=stage,
                    stage_index=index,
                    label=request["label"],
                )
                if result is None:
                    terminal_reason = error or f"stage {stage.id} produced no result"
                    break
                stage_results.append((stage.id, stage.kind, result))
                if exit_code != 0 and result["status"] == "passed":
                    terminal_reason = (
                        f"stage {stage.id} exited {exit_code} after reporting passed"
                    )
                    break
                if result["status"] != "passed":
                    terminal_reason = result.get("summary") or f"stage {stage.id} failed"
                    break
            final = aggregate_run_result(
                task=task,
                run_id=run_id,
                task_digest=request["task_sha256"],
                candidate_digest=request["candidate_sha256"],
                stage_results=stage_results,
                terminal_reason=terminal_reason,
            )
            terminal_state = final["outcome"]
            self.store.atomic_json(run_dir / "result.json", final, mode=0o600)
            rebuild_frontier(self.store, task)
            self.store.update_state(
                run_id,
                terminal_state,
                state=terminal_state,
                stage_id=None,
                stage_kind=None,
                stage_index=None,
                reason=terminal_reason,
            )
        except asyncio.CancelledError:
            await self._interrupt_process(run_id)
            stop = self._stop_reasons.get(run_id, "interrupted")
            reason = (
                "run cancelled by request"
                if stop == "cancelled"
                else "kernel-infrad stopped before the run became terminal"
            )
            final = aggregate_run_result(
                task=task,
                run_id=run_id,
                task_digest=request["task_sha256"],
                candidate_digest=request["candidate_sha256"],
                stage_results=stage_results,
                terminal_reason=reason,
            )
            final["outcome"] = stop
            final["frontier_eligible"] = False
            self.store.atomic_json(run_dir / "result.json", final, mode=0o600)
            self.store.update_state(
                run_id,
                stop,
                state=stop,
                reason=reason,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            final = aggregate_run_result(
                task=task,
                run_id=run_id,
                task_digest=request["task_sha256"],
                candidate_digest=request["candidate_sha256"],
                stage_results=stage_results,
                terminal_reason=reason,
            )
            final["outcome"] = "infra_error"
            final["frontier_eligible"] = False
            self.store.atomic_json(run_dir / "result.json", final, mode=0o600)
            self.store.update_state(
                run_id,
                "infra_error",
                state="infra_error",
                reason=reason,
            )
        finally:
            self._processes.pop(run_id, None)
            self._tasks.pop(run_id, None)
            self._stop_reasons.pop(run_id, None)
            self._done.setdefault(run_id, asyncio.Event()).set()

    async def _run_stage(
        self,
        *,
        run_id: str,
        task: TaskSpec,
        stage: StageSpec,
        stage_index: int,
        label: str,
    ) -> tuple[dict[str, Any] | None, int, str | None]:
        run_dir = self.store.run_dir(run_id)
        stage_dir = run_dir / "stages" / stage.id
        stage_dir.mkdir(parents=True, exist_ok=False)
        result_path = stage_dir / "result.json"
        stdout_path = stage_dir / "stdout.log"
        stderr_path = stage_dir / "stderr.log"
        started_at = utc_now()
        self.store.update_state(
            run_id,
            "stage_submitted",
            state="submitting",
            stage_id=stage.id,
            stage_kind=stage.kind,
            stage_index=stage_index,
            broker_job_id=None,
            gpu_ids=[],
            reason=None,
        )

        stage_environment = self._stage_environment(
            run_id=run_id,
            stage=stage,
            stage_dir=stage_dir,
            result_path=result_path,
        )
        if stage.execution == "broker":
            command = self._broker_command(
                task=task,
                stage=stage,
                label=label,
                environment=stage_environment,
            )
            process_environment = None
        else:
            command = list(stage.command)
            process_environment = {**os.environ, **stage_environment}
            self.store.update_state(
                run_id,
                "service_request_started",
                state="running",
            )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(stage.cwd),
            env=process_environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._processes[run_id] = process
        assert process.stdout is not None and process.stderr is not None
        stdout_task = asyncio.create_task(
            self._pump(process.stdout, stdout_path),
            name=f"{run_id}-{stage.id}-stdout",
        )
        if stage.execution == "broker":
            stderr_task = asyncio.create_task(
                self._pump_broker_stderr(run_id, process.stderr, stderr_path),
                name=f"{run_id}-{stage.id}-stderr",
            )
        else:
            stderr_task = asyncio.create_task(
                self._pump(process.stderr, stderr_path),
                name=f"{run_id}-{stage.id}-stderr",
            )
        try:
            exit_code = await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
        except asyncio.CancelledError:
            for pump in (stdout_task, stderr_task):
                pump.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        finally:
            if self._processes.get(run_id) is process and process.returncode is not None:
                self._processes.pop(run_id, None)

        error: str | None = None
        result: dict[str, Any] | None = None
        try:
            result = load_stage_result(result_path, task)
        except ContractError as exc:
            error = str(exc)
        receipt = {
            "schema": "kernelinfra.stage-receipt.v1",
            "run_id": run_id,
            "stage_id": stage.id,
            "stage_kind": stage.kind,
            "execution": stage.execution,
            "judge_identity": stage.judge_identity,
            "started_at": started_at,
            "finished_at": utc_now(),
            "exit_code": exit_code,
            "broker_job_id": self.store.read_state(run_id).get("broker_job_id"),
            "gpu_ids": self.store.read_state(run_id).get("gpu_ids", []),
            "judge_result_valid": result is not None,
            "error": error,
        }
        self.store.atomic_json(stage_dir / "receipt.json", receipt, mode=0o600)
        self.store.update_state(
            run_id,
            "stage_finished",
            state="stage_finished",
            reason=error,
        )
        return result, exit_code, error

    def _broker_command(
        self,
        *,
        task: TaskSpec,
        stage: StageSpec,
        label: str,
        environment: dict[str, str],
    ) -> list[str]:
        resources = stage.resources
        assert resources is not None
        command = [
            str(self.gpu_run),
            "--socket",
            str(self.broker_socket),
            "--label",
            f"ki:{task.task_id}:{stage.id}:{label}"[:160],
            "--mode",
            resources.mode,
            "--gpu-count",
            str(resources.gpu_count),
            "--estimate",
            "unknown" if resources.estimate_s is None else str(resources.estimate_s),
            "--run-timeout",
            str(resources.run_timeout_s),
        ]
        if resources.queue_timeout_s is not None:
            command.extend(["--queue-timeout", str(resources.queue_timeout_s)])
        for key, value in environment.items():
            command.extend(["--env", f"{key}={value}"])
        command.extend(["--", *stage.command])
        return command

    def _stage_environment(
        self,
        *,
        run_id: str,
        stage: StageSpec,
        stage_dir: Path,
        result_path: Path,
    ) -> dict[str, str]:
        return {
            "PYTHONUNBUFFERED": "1",
            "KERNELINFRA_RUN_ID": run_id,
            "KERNELINFRA_TASK": str(self.store.run_dir(run_id) / "task.json"),
            "KERNELINFRA_CANDIDATE_DIR": str(
                self.store.run_dir(run_id) / "candidate"
            ),
            "KERNELINFRA_STAGE_ID": stage.id,
            "KERNELINFRA_STAGE_KIND": stage.kind,
            "KERNELINFRA_STAGE_DIR": str(stage_dir),
            "KERNELINFRA_RESULT": str(result_path),
        }

    async def _pump(self, reader: asyncio.StreamReader, path: Path) -> None:
        with path.open("ab") as handle:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                handle.write(chunk)
                handle.flush()

    async def _pump_broker_stderr(
        self,
        run_id: str,
        reader: asyncio.StreamReader,
        path: Path,
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
                        run_id,
                        "broker_accepted",
                        state="queued",
                        broker_job_id=accepted.group("job"),
                    )
                    continue
                if "[gpu-run] queued " in text:
                    self.store.update_state(run_id, "broker_queued", state="queued")
                    continue
                gpu_ids = _GPU_IDS.search(text)
                if gpu_ids:
                    ids = [int(value) for value in gpu_ids.group("ids").split(",")]
                    self.store.update_state(
                        run_id,
                        "broker_started",
                        state="running",
                        gpu_ids=ids,
                    )

    async def _interrupt_process(self, run_id: str) -> None:
        process = self._processes.get(run_id)
        if process is None or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
            return
        except asyncio.TimeoutError:
            pass
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
