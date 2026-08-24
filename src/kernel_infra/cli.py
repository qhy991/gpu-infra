"""Command-line client and daemon."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import socket
import sys
from pathlib import Path
from typing import Any

from .contracts import ContractError, load_task
from .runner import RunManager
from .server import KernelInfraServer
from .store import RunStore, TERMINAL_STATES

DEFAULT_SOCKET = Path("/tmp/kernel-infra.sock")
DEFAULT_BROKER_SOCKET = Path("/tmp/agent-gpu-broker.sock")
DEFAULT_STATE_DIR = Path.home() / ".local/share/kernel-infra"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kernelctl")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the persistent control daemon")
    serve.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    serve.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    serve.add_argument("--broker-socket", type=Path, default=DEFAULT_BROKER_SOCKET)
    serve.add_argument("--gpu-run", type=Path, default=Path("gpu-run"))

    check = sub.add_parser("task-check", help="validate one task contract")
    check.add_argument("task", type=Path)

    submit = sub.add_parser("submit", help="snapshot and submit one candidate")
    _client_socket(submit)
    submit.add_argument("--task", type=Path, required=True)
    submit.add_argument("--label")
    submit.add_argument("--wait", action="store_true")
    submit.add_argument("candidate", type=Path)

    many = sub.add_parser("submit-many", help="submit candidates in parallel")
    _client_socket(many)
    many.add_argument("--task", type=Path, required=True)
    many.add_argument("--label-prefix")
    many.add_argument("candidates", nargs="+", type=Path)

    status = sub.add_parser("status", help="show run lifecycle")
    _client_socket(status)
    status.add_argument("run_id", nargs="?")
    status.add_argument("--task-id")
    status.add_argument("--json", action="store_true")

    wait = sub.add_parser("wait", help="wait for one run")
    _client_socket(wait)
    wait.add_argument("run_id")
    wait.add_argument("--timeout", type=float)
    wait.add_argument("--json", action="store_true")

    cancel = sub.add_parser("cancel", help="cancel one queued or running run")
    _client_socket(cancel)
    cancel.add_argument("run_id")

    frontier = sub.add_parser("frontier", help="rebuild a task frontier")
    _client_socket(frontier)
    frontier.add_argument("--task", type=Path, required=True)
    frontier.add_argument("--json", action="store_true")
    return parser


def _client_socket(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return asyncio.run(_serve(args))
    if args.command == "task-check":
        return _task_check(args)
    if args.command == "submit":
        return _submit(args)
    if args.command == "submit-many":
        return _submit_many(args)
    if args.command == "status":
        return _status(args)
    if args.command == "wait":
        return _wait(args)
    if args.command == "cancel":
        return _cancel(args)
    if args.command == "frontier":
        return _frontier(args)
    raise AssertionError(args.command)


async def _serve(args: argparse.Namespace) -> int:
    gpu_run = args.gpu_run.expanduser()
    if not gpu_run.is_absolute():
        import shutil

        resolved = shutil.which(str(gpu_run))
        if resolved is None:
            print(f"kernelctl: gpu-run not found: {gpu_run}", file=sys.stderr)
            return 1
        gpu_run = Path(resolved)
    manager = RunManager(
        store=RunStore(args.state_dir),
        gpu_run=gpu_run,
        broker_socket=args.broker_socket,
    )
    server = KernelInfraServer(manager, args.socket)
    try:
        recovered = await server.start()
    except Exception as exc:
        print(f"kernelctl: {exc}", file=sys.stderr)
        return 1
    print(
        f"kernel-infrad: serving {server.socket_path} state={manager.store.root} "
        f"recovered_interrupted={recovered}",
        flush=True,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        await stop.wait()
    finally:
        await server.close()
    return 0


def _task_check(args: argparse.Namespace) -> int:
    try:
        task = load_task(args.task)
    except ContractError as exc:
        print(f"kernelctl: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "schema": task.raw["schema"],
                "task_id": task.task_id,
                "task_sha256": task.digest,
                "workloads": list(task.workloads),
                "stages": [
                    {
                        "id": stage.id,
                        "kind": stage.kind,
                        "execution": stage.execution,
                        "mode": (
                            stage.resources.mode if stage.resources is not None else None
                        ),
                        "gpu_count": (
                            stage.resources.gpu_count
                            if stage.resources is not None
                            else None
                        ),
                        "judge_identity": stage.judge_identity,
                    }
                    for stage in task.stages
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _submit(args: argparse.Namespace) -> int:
    response = _request(
        args.socket,
        {
            "op": "submit",
            "task": str(args.task.expanduser().resolve()),
            "candidate": str(args.candidate.expanduser().resolve()),
            "label": args.label,
        },
    )
    if response is None:
        return 1
    run = response["run"]
    print(run["run_id"])
    if args.wait:
        return _wait_for_id(args.socket, run["run_id"], None, json_output=False)
    return 0


def _submit_many(args: argparse.Namespace) -> int:
    response = _request(
        args.socket,
        {
            "op": "submit_many",
            "task": str(args.task.expanduser().resolve()),
            "candidates": [str(path.expanduser().resolve()) for path in args.candidates],
            "label_prefix": args.label_prefix,
        },
    )
    if response is None:
        return 1
    for run in response["runs"]:
        print(run["run_id"])
    return 0


def _status(args: argparse.Namespace) -> int:
    response = _request(
        args.socket,
        {"op": "status", "run_id": args.run_id, "task_id": args.task_id},
    )
    if response is None:
        return 1
    runs = response["runs"]
    if args.json:
        print(json.dumps(runs, indent=2, ensure_ascii=False))
    else:
        _print_runs(runs)
    return 0


def _wait(args: argparse.Namespace) -> int:
    return _wait_for_id(args.socket, args.run_id, args.timeout, args.json)


def _wait_for_id(
    socket_path: Path, run_id: str, timeout: float | None, json_output: bool
) -> int:
    response = _request(
        socket_path,
        {"op": "wait", "run_id": run_id, "timeout": timeout},
    )
    if response is None:
        return 1
    run = response["run"]
    if json_output:
        print(json.dumps(run, indent=2, ensure_ascii=False))
    else:
        _print_runs([run])
    return 0 if run["state"] in TERMINAL_STATES else 3


def _cancel(args: argparse.Namespace) -> int:
    response = _request(args.socket, {"op": "cancel", "run_id": args.run_id})
    if response is None:
        return 1
    if not response["cancelled"]:
        print("kernelctl: run is already terminal or not active", file=sys.stderr)
        return 1
    return 0


def _frontier(args: argparse.Namespace) -> int:
    response = _request(
        args.socket,
        {"op": "frontier", "task": str(args.task.expanduser().resolve())},
    )
    if response is None:
        return 1
    frontier = response["frontier"]
    if args.json:
        print(json.dumps(frontier, indent=2, ensure_ascii=False))
    else:
        print(
            f"FRONTIER task={frontier['task_id']} digest={frontier['task_sha256'][:12]}"
        )
        for workload_id, cell in sorted(frontier["cells"].items()):
            print(
                f"  {workload_id}: run={cell['run_id']} "
                f"candidate={cell['candidate_ms']:.6f}ms "
                f"speedup={cell['speedup']:.6f}x"
            )
        print("DECISIONS")
        for decision in frontier["decisions"]:
            reason = f" reason={decision['reason']}" if decision.get("reason") else ""
            print(
                f"  {decision['run_id']}: {decision['decision']}"
                f" validity={decision['validity']}{reason}"
            )
        print(f"path={frontier['path']}")
    return 0


def _request(socket_path: Path, value: dict[str, Any]) -> dict[str, Any] | None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(socket_path.expanduser()))
        connection = client.makefile("rwb")
        with client, connection:
            connection.write((json.dumps(value) + "\n").encode())
            connection.flush()
            response = json.loads(connection.readline())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"kernelctl: cannot reach daemon at {socket_path}: {exc}", file=sys.stderr)
        return None
    if not response.get("ok"):
        print(f"kernelctl: {response.get('error', 'request failed')}", file=sys.stderr)
        return None
    return response


def _print_runs(runs: list[dict[str, Any]]) -> None:
    if not runs:
        print("-")
        return
    for run in runs:
        stage = run.get("stage_id") or "-"
        broker = run.get("broker_job_id") or "-"
        gpu_ids = ",".join(map(str, run.get("gpu_ids", []))) or "-"
        reason = f" reason={run['reason']}" if run.get("reason") else ""
        print(
            f"{run['run_id']} state={run['state']} stage={stage} "
            f"broker={broker} gpus={gpu_ids}{reason}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
