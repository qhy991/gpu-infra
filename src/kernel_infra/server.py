"""Unix-socket JSONL API for non-blocking kernel runs."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

from .candidate import validate_candidate
from .contracts import ContractError, load_task
from .frontier import rebuild_frontier
from .runner import RunManager


class KernelInfraServer:
    def __init__(self, manager: RunManager, socket_path: Path) -> None:
        self.manager = manager
        self.socket_path = socket_path.expanduser().resolve()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        recovered = self.manager.recover_interrupted()
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
        return recovered

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        await self.manager.close()
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
            state = self.manager.submit(
                task_path=Path(request["task"]),
                candidate=Path(request["candidate"]),
                label=request.get("label"),
            )
            return {"ok": True, "run": state}
        if operation == "submit_many":
            candidates = request.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                raise ValueError("candidates must be a non-empty list")
            # Fail before accepting any run when a batch contains an invalid
            # candidate. Snapshotting repeats this check to close the mutation
            # window for cooperating clients.
            load_task(Path(request["task"]))
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
        raise ValueError(f"unknown operation: {operation!r}")
