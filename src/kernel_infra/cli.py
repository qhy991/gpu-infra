"""Command-line client and daemon."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

from .contracts import ContractError, load_task
from .runner import RunManager
from .server import KernelInfraServer
from .service_attestation import atomic_json, build_service_receipt
from .service_contracts import load_service_spec
from .service_store import SERVICE_TERMINAL_STATES, ServiceStore
from .services import ServiceManager
from .store import RunStore, TERMINAL_STATES

DEFAULT_SOCKET = Path("/tmp/kernel-infra.sock")
DEFAULT_BROKER_SOCKET = Path("/tmp/agent-gpu-broker.sock")
DEFAULT_STATE_DIR = Path.home() / ".local/share/kernel-infra"


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value}") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kernelctl")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the persistent control daemon")
    serve.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    serve.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    serve.add_argument("--broker-socket", type=Path, default=DEFAULT_BROKER_SOCKET)
    serve.add_argument("--gpu-run", type=Path, default=Path("gpu-run"))
    serve.add_argument("--local-capacity", type=_positive_int, default=2)

    check = sub.add_parser("task-check", help="validate one task contract")
    check.add_argument("task", type=Path)

    attest = sub.add_parser(
        "service-attest", help="attest one broker-held evaluator service"
    )
    attest.add_argument("--broker-socket", type=Path, default=DEFAULT_BROKER_SOCKET)
    attest.add_argument("--broker-job-id", required=True)
    attest.add_argument("--broker-admission-receipt", type=Path, required=True)
    attest.add_argument("--service-url", required=True)
    attest.add_argument("--service-identity", required=True)
    attest.add_argument("--source-root", type=Path, required=True)
    attest.add_argument("--out", type=Path, required=True)

    service_check = sub.add_parser(
        "service-check", help="validate one managed service contract"
    )
    service_check.add_argument("spec", type=Path)

    service_start = sub.add_parser(
        "service-start", help="start one broker-held evaluator deployment"
    )
    _client_socket(service_start)
    service_start.add_argument("--wait", action="store_true")
    service_start.add_argument("spec", type=Path)

    service_status = sub.add_parser(
        "service-status", help="show managed evaluator deployments"
    )
    _client_socket(service_status)
    service_status.add_argument("deployment_id", nargs="?")
    service_status.add_argument("--service-id")
    service_status.add_argument("--json", action="store_true")

    service_wait = sub.add_parser(
        "service-wait", help="wait until a deployment is ready or terminal"
    )
    _client_socket(service_wait)
    service_wait.add_argument("deployment_id")
    service_wait.add_argument("--timeout", type=float)
    service_wait.add_argument("--json", action="store_true")

    service_stop = sub.add_parser(
        "service-stop", help="stop one managed evaluator deployment"
    )
    _client_socket(service_stop)
    service_stop.add_argument("deployment_id")

    service_bind = sub.add_parser(
        "service-bind-task",
        help="materialize one task template from a ready service deployment",
    )
    _client_socket(service_bind)
    service_bind.add_argument("--deployment", required=True)
    service_bind.add_argument("--template", type=Path, required=True)
    service_bind.add_argument("--out", type=Path, required=True)
    service_bind.add_argument("--binding-out", type=Path)

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
    if args.command == "service-attest":
        return _service_attest(args)
    if args.command == "service-check":
        return _service_check(args)
    if args.command == "service-start":
        return _service_start(args)
    if args.command == "service-status":
        return _service_status(args)
    if args.command == "service-wait":
        return _service_wait(args)
    if args.command == "service-stop":
        return _service_stop(args)
    if args.command == "service-bind-task":
        return _service_bind_task(args)
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
        local_capacity=args.local_capacity,
    )
    services = ServiceManager(
        store=ServiceStore(args.state_dir),
        gpu_run=gpu_run,
        broker_socket=args.broker_socket,
        run_store=manager.store,
    )
    server = KernelInfraServer(manager, services, args.socket)
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
                        "service_deployment": stage.service_deployment_id,
                    }
                    for stage in task.stages
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _service_attest(args: argparse.Namespace) -> int:
    try:
        receipt = build_service_receipt(
            broker_socket=args.broker_socket,
            broker_job_id=args.broker_job_id,
            broker_admission_receipt=args.broker_admission_receipt,
            service_url=args.service_url,
            service_identity=args.service_identity,
            source_root=args.source_root,
        )
        output = args.out.expanduser().resolve()
        atomic_json(output, receipt)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"kernelctl: {exc}", file=sys.stderr)
        return 1
    print(str(output))
    return 0


def _service_check(args: argparse.Namespace) -> int:
    try:
        spec = load_service_spec(args.spec)
    except ContractError as exc:
        print(f"kernelctl: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "schema": spec.raw["schema"],
                "service_id": spec.service_id,
                "service_sha256": spec.digest,
                "owner": spec.owner,
                "service_url": spec.service_url,
                "source_root": str(spec.source_root),
                "cwd": str(spec.cwd),
                "command": list(spec.command),
                "env_keys": sorted(spec.env),
                "gpu_count": spec.resources.gpu_count,
                "idle_grace_s": spec.idle_grace_s,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _service_start(args: argparse.Namespace) -> int:
    response = _request(
        args.socket,
        {"op": "service_start", "spec": str(args.spec.expanduser().resolve())},
    )
    if response is None:
        return 1
    state = response["service"]
    print(state["deployment_id"])
    if args.wait:
        return _service_wait_for_id(
            args.socket, state["deployment_id"], None, json_output=False
        )
    return 0


def _service_status(args: argparse.Namespace) -> int:
    response = _request(
        args.socket,
        {
            "op": "service_status",
            "deployment_id": args.deployment_id,
            "service_id": args.service_id,
        },
    )
    if response is None:
        return 1
    states = response["services"]
    if args.json:
        print(json.dumps(states, indent=2, ensure_ascii=False))
    else:
        _print_services(states)
    return 0


def _service_wait(args: argparse.Namespace) -> int:
    return _service_wait_for_id(
        args.socket, args.deployment_id, args.timeout, args.json
    )


def _service_wait_for_id(
    socket_path: Path,
    deployment_id: str,
    timeout: float | None,
    json_output: bool,
) -> int:
    response = _request(
        socket_path,
        {
            "op": "service_wait",
            "deployment_id": deployment_id,
            "timeout": timeout,
        },
    )
    if response is None:
        return 1
    state = response["service"]
    if json_output:
        print(json.dumps(state, indent=2, ensure_ascii=False))
    else:
        _print_services([state])
    if state["state"] == "ready":
        return 0
    return 1 if state["state"] in SERVICE_TERMINAL_STATES else 3


def _service_stop(args: argparse.Namespace) -> int:
    response = _request(
        args.socket,
        {"op": "service_stop", "deployment_id": args.deployment_id},
    )
    if response is None:
        return 1
    if not response["stopped"]:
        print("kernelctl: service is already terminal", file=sys.stderr)
        return 1
    return 0


def _service_bind_task(args: argparse.Namespace) -> int:
    output = args.out.expanduser().resolve()
    binding_output = (
        args.binding_out.expanduser().resolve()
        if args.binding_out is not None
        else Path(str(output) + ".binding.json")
    )
    if output.exists():
        print(f"kernelctl: refusing to overwrite task output: {output}", file=sys.stderr)
        return 1
    if binding_output.exists():
        print(
            f"kernelctl: refusing to overwrite task binding receipt: {binding_output}",
            file=sys.stderr,
        )
        return 1
    response = _request(
        args.socket,
        {
            "op": "service_bind_task",
            "deployment_id": args.deployment,
            "template": str(args.template.expanduser().resolve()),
            "output": str(output),
            "binding_output": str(binding_output),
        },
    )
    if response is None:
        return 1
    # Write provenance first: a crash cannot leave an executable task without
    # its binding receipt. Both writes are individually atomic and no-overwrite.
    try:
        _atomic_new_json(binding_output, response["binding"])
        _atomic_new_json(output, response["task"])
    except FileExistsError as exc:
        print(f"kernelctl: refusing to overwrite output: {exc.filename}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"kernelctl: cannot write bound task outputs: {exc}", file=sys.stderr)
        return 1
    print(str(output))
    print(str(binding_output))
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


def _atomic_new_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.link(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _print_runs(runs: list[dict[str, Any]]) -> None:
    if not runs:
        print("-")
        return
    for run in runs:
        stage = run.get("stage_id") or "-"
        broker = run.get("broker_job_id") or "-"
        gpu_ids = ",".join(map(str, run.get("gpu_ids", []))) or "-"
        services = ",".join(run.get("service_deployment_ids", [])) or "-"
        reason = f" reason={run['reason']}" if run.get("reason") else ""
        print(
            f"{run['run_id']} state={run['state']} stage={stage} "
            f"broker={broker} gpus={gpu_ids} services={services}{reason}"
        )


def _print_services(states: list[dict[str, Any]]) -> None:
    if not states:
        print("-")
        return
    for state in states:
        broker = state.get("broker_job_id") or "-"
        gpu_ids = ",".join(map(str, state.get("gpu_ids", []))) or "-"
        receipt = state.get("deployment_receipt") or "-"
        consumers = state.get("active_consumer_count", 0)
        reason = f" reason={state['reason']}" if state.get("reason") else ""
        print(
            f"{state['deployment_id']} state={state['state']} "
            f"broker={broker} gpus={gpu_ids} consumers={consumers} "
            f"receipt={receipt}{reason}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
