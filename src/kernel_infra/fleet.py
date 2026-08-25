"""Thin cross-host routing and immutable bundle transport."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable

from .candidate import hash_snapshot, snapshot_candidate
from .contracts import ContractError, TaskSpec, digest_json, load_task
from .store import RunStore, utc_now

FLEET_SCHEMA = "kernelinfra.fleet.v1"
BUNDLE_SCHEMA = "kernelinfra.fleet-bundle.v1"
ROUTE_SCHEMA = "kernelinfra.route-receipt.v1"
RECEIVE_SCHEMA = "kernelinfra.fleet-receive.v1"
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_SSH = re.compile(r"^[A-Za-z0-9_.@:-]+$")
_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")


@dataclass(frozen=True)
class FleetNode:
    node_id: str
    ssh_host: str
    kernelctl: str
    socket: str
    inbox: str
    capabilities: frozenset[str]


@dataclass(frozen=True)
class FleetCatalog:
    nodes: tuple[FleetNode, ...]
    connect_timeout_s: float
    command_timeout_s: float
    source_path: Path
    digest: str


class FleetSelectionError(RuntimeError):
    def __init__(self, message: str, decisions: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.decisions = decisions


def _positive(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{where} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ContractError(f"{where} must be a finite positive number")
    return result


def _safe_remote_path(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _REMOTE_PATH.fullmatch(value):
        raise ContractError(
            f"{where} must be an absolute remote path without whitespace"
        )
    return value


def load_fleet_catalog(path: Path) -> FleetCatalog:
    source = path.expanduser().resolve()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"fleet catalog not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid fleet catalog JSON at {source}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) - {
        "schema",
        "nodes",
        "connect_timeout_s",
        "command_timeout_s",
    }:
        raise ContractError("fleet catalog has unexpected fields")
    if raw.get("schema") != FLEET_SCHEMA:
        raise ContractError(f"fleet catalog schema must be {FLEET_SCHEMA!r}")
    nodes_value = raw.get("nodes")
    if not isinstance(nodes_value, list) or not nodes_value:
        raise ContractError("fleet catalog nodes must be a non-empty list")
    nodes: list[FleetNode] = []
    for index, value in enumerate(nodes_value):
        where = f"fleet.nodes[{index}]"
        if not isinstance(value, dict) or set(value) != {
            "id",
            "ssh",
            "kernelctl",
            "socket",
            "inbox",
            "capabilities",
        }:
            raise ContractError(f"{where} has invalid fields")
        node_id = value["id"]
        if not isinstance(node_id, str) or not _ID.fullmatch(node_id):
            raise ContractError(f"{where}.id is invalid")
        ssh_host = value["ssh"]
        if not isinstance(ssh_host, str) or not _SSH.fullmatch(ssh_host):
            raise ContractError(f"{where}.ssh is unsafe")
        capabilities = value["capabilities"]
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) and _ID.fullmatch(item) for item in capabilities
        ):
            raise ContractError(f"{where}.capabilities must be identifiers")
        if len(set(capabilities)) != len(capabilities):
            raise ContractError(f"{where}.capabilities must be unique")
        nodes.append(
            FleetNode(
                node_id=node_id,
                ssh_host=ssh_host,
                kernelctl=_safe_remote_path(value["kernelctl"], f"{where}.kernelctl"),
                socket=_safe_remote_path(value["socket"], f"{where}.socket"),
                inbox=_safe_remote_path(value["inbox"], f"{where}.inbox"),
                capabilities=frozenset(capabilities),
            )
        )
    if len({node.node_id for node in nodes}) != len(nodes):
        raise ContractError("fleet node ids must be unique")
    return FleetCatalog(
        nodes=tuple(nodes),
        connect_timeout_s=_positive(
            raw.get("connect_timeout_s", 8), "connect_timeout_s"
        ),
        command_timeout_s=_positive(
            raw.get("command_timeout_s", 30), "command_timeout_s"
        ),
        source_path=source,
        digest=digest_json(raw),
    )


def _ssh_base(node: FleetNode, catalog: FleetCatalog) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, math.ceil(catalog.connect_timeout_s))}",
        node.ssh_host,
    ]


def probe_node(
    node: FleetNode,
    catalog: FleetCatalog,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    started = time.monotonic()
    command = shlex.join(
        [node.kernelctl, "node-status", "--socket", node.socket, "--json"]
    )
    try:
        completed = run(
            [*_ssh_base(node, catalog), command],
            capture_output=True,
            text=True,
            check=False,
            timeout=catalog.command_timeout_s,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"ssh exit {completed.returncode}: {completed.stderr[-1000:].strip()}"
            )
        status = json.loads(completed.stdout)
        if not isinstance(status, dict) or status.get("schema") != "kernelinfra.node-status.v1":
            raise RuntimeError("remote node-status returned an invalid payload")
        return {
            "schema": "kernelinfra.fleet-node-observation.v1",
            "node_id": node.node_id,
            "ssh": node.ssh_host,
            "status": "ok",
            "capabilities": sorted(node.capabilities),
            "elapsed_seconds": time.monotonic() - started,
            "node": status,
            "error": None,
        }
    except (OSError, subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as exc:
        return {
            "schema": "kernelinfra.fleet-node-observation.v1",
            "node_id": node.node_id,
            "ssh": node.ssh_host,
            "status": "unknown",
            "capabilities": sorted(node.capabilities),
            "elapsed_seconds": time.monotonic() - started,
            "node": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def probe_fleet(
    catalog: FleetCatalog,
    *,
    probe: Callable[[FleetNode, FleetCatalog], dict[str, Any]] = probe_node,
) -> list[dict[str, Any]]:
    observations: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, len(catalog.nodes))
    ) as executor:
        futures = {
            executor.submit(probe, node, catalog): node.node_id
            for node in catalog.nodes
        }
        for future, node_id in futures.items():
            try:
                observations[node_id] = future.result()
            except Exception as exc:
                node = next(item for item in catalog.nodes if item.node_id == node_id)
                observations[node_id] = {
                    "schema": "kernelinfra.fleet-node-observation.v1",
                    "node_id": node_id,
                    "ssh": node.ssh_host,
                    "status": "unknown",
                    "capabilities": sorted(node.capabilities),
                    "elapsed_seconds": 0.0,
                    "node": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
    return [observations[node.node_id] for node in catalog.nodes]


def required_deployments(task: TaskSpec) -> set[str]:
    return {
        stage.service_deployment_id
        for stage in task.stages
        if stage.service_deployment_id is not None
    }


def select_node(
    *,
    catalog: FleetCatalog,
    observations: list[dict[str, Any]],
    required_capabilities: set[str],
    required_deployments: set[str],
    min_free_bytes: int,
) -> tuple[FleetNode, dict[str, Any], list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []
    eligible: list[tuple[tuple[Any, ...], FleetNode, dict[str, Any]]] = []
    by_id = {node.node_id: node for node in catalog.nodes}
    for observation in observations:
        node = by_id[observation["node_id"]]
        reasons: list[str] = []
        status = observation.get("node")
        if observation.get("status") != "ok" or not isinstance(status, dict):
            reasons.append("probe_unknown")
        else:
            missing_capabilities = sorted(required_capabilities - node.capabilities)
            if missing_capabilities:
                reasons.append("missing_capabilities=" + ",".join(missing_capabilities))
            ready = set(status.get("ready_deployments", []))
            missing_deployments = sorted(required_deployments - ready)
            if missing_deployments:
                reasons.append("missing_deployments=" + ",".join(missing_deployments))
            if int(status.get("disk", {}).get("free_bytes", 0)) < min_free_bytes:
                reasons.append("insufficient_disk")
            broker = status.get("broker", {})
            if broker.get("probe_error"):
                reasons.append("broker_probe_error")
        decision = {
            "node_id": node.node_id,
            "eligible": not reasons,
            "reasons": reasons,
        }
        decisions.append(decision)
        if not reasons:
            broker = status["broker"]
            idle = sum(
                1 for gpu in broker.get("gpus", []) if gpu.get("state") == "idle"
            )
            rank = (
                len(broker.get("queue", [])),
                -idle,
                len(status.get("active_runs", [])),
                node.node_id,
            )
            eligible.append((rank, node, observation))
    if not eligible:
        summary = "; ".join(
            f"{item['node_id']}:{','.join(item['reasons']) or 'ineligible'}"
            for item in decisions
        )
        raise FleetSelectionError(f"no eligible fleet node: {summary}", decisions)
    _rank, node, observation = min(eligible, key=lambda item: item[0])
    return node, observation, decisions


def _fleet_task_is_relocatable(task: TaskSpec) -> None:
    for index, stage in enumerate(task.raw["stages"]):
        cwd = stage.get("judge", {}).get("cwd") if isinstance(stage, dict) else None
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ContractError(
                f"fleet task stage {index} judge.cwd must be an absolute remote path"
            )


def create_fleet_bundle(
    *, task_path: Path, candidate: Path, workspace: Path
) -> tuple[TaskSpec, dict[str, Any], Path]:
    task = load_task(task_path)
    _fleet_task_is_relocatable(task)
    payload = workspace / "payload"
    payload.mkdir()
    RunStore.atomic_json(payload / "task.json", task.raw, mode=0o600)
    candidate_sha256 = snapshot_candidate(candidate, payload / "candidate")
    identity = {
        "task_sha256": task.digest,
        "candidate_sha256": candidate_sha256,
    }
    bundle_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "bundle_id": bundle_id,
        "task_id": task.task_id,
        **identity,
    }
    RunStore.atomic_json(payload / "manifest.json", manifest, mode=0o600)
    archive = workspace / f"{bundle_id}.tar"
    with tarfile.open(archive, "w") as handle:
        for path in sorted(payload.rglob("*")):
            handle.add(path, arcname=path.relative_to(payload).as_posix(), recursive=False)
    return task, manifest, archive


def _safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"unsafe fleet archive path: {name!r}")
    if path.parts[0] not in {"manifest.json", "task.json", "candidate"}:
        raise RuntimeError(f"unexpected fleet archive path: {name!r}")
    if path.parts[0] in {"manifest.json", "task.json"} and len(path.parts) != 1:
        raise RuntimeError(f"invalid fleet archive root file: {name!r}")
    return path


def receive_fleet_bundle(
    *, stream: BinaryIO, inbox: Path, expected_bundle_id: str
) -> tuple[Path, dict[str, Any], bool]:
    if not re.fullmatch(r"[0-9a-f]{32}", expected_bundle_id):
        raise RuntimeError("invalid expected fleet bundle id")
    root = inbox.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".incoming-", dir=root))
    total = 0
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=stream, mode="r|*") as archive:
            for member in archive:
                relative = _safe_archive_path(member.name)
                normalized = relative.as_posix()
                if normalized in seen:
                    raise RuntimeError(f"duplicate fleet archive path: {normalized}")
                seen.add(normalized)
                target = staging.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                if not member.isfile():
                    raise RuntimeError(f"fleet archive contains non-file: {normalized}")
                total += member.size
                if total > MAX_BUNDLE_BYTES:
                    raise RuntimeError("fleet bundle exceeds maximum extracted bytes")
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"cannot read fleet archive member: {normalized}")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o600)
        manifest_path = staging / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid fleet bundle manifest: {exc}") from exc
        if not isinstance(manifest, dict) or set(manifest) != {
            "schema",
            "bundle_id",
            "task_id",
            "task_sha256",
            "candidate_sha256",
        }:
            raise RuntimeError("fleet bundle manifest has invalid fields")
        if manifest.get("schema") != BUNDLE_SCHEMA:
            raise RuntimeError("fleet bundle manifest schema drift")
        if manifest.get("bundle_id") != expected_bundle_id:
            raise RuntimeError("fleet bundle id disagrees with expected id")
        task = load_task(staging / "task.json")
        candidate_dir = staging / "candidate"
        if not candidate_dir.is_dir():
            raise RuntimeError("fleet bundle candidate directory is missing")
        candidate_sha256 = hash_snapshot(candidate_dir)
        if task.digest != manifest["task_sha256"]:
            raise RuntimeError("fleet bundle task digest drift")
        if task.task_id != manifest["task_id"]:
            raise RuntimeError("fleet bundle task id drift")
        if candidate_sha256 != manifest["candidate_sha256"]:
            raise RuntimeError("fleet bundle candidate digest drift")
        identity = {
            "task_sha256": task.digest,
            "candidate_sha256": candidate_sha256,
        }
        calculated = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]
        if calculated != expected_bundle_id:
            raise RuntimeError("fleet bundle content address drift")
        target = root / expected_bundle_id
        if target.exists():
            existing = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            if existing != manifest:
                raise RuntimeError("existing fleet bundle manifest disagrees")
            existing_task = load_task(target / "task.json")
            if (
                existing_task.digest != task.digest
                or hash_snapshot(target / "candidate") != candidate_sha256
            ):
                raise RuntimeError("existing fleet bundle content drift")
            return target, manifest, True
        os.rename(staging, target)
        staging = target
        return target, manifest, False
    finally:
        if staging.exists() and staging.name.startswith(".incoming-"):
            shutil.rmtree(staging)


def submit_bundle_to_node(
    *,
    node: FleetNode,
    catalog: FleetCatalog,
    archive_path: Path,
    bundle_id: str,
    label: str,
) -> dict[str, Any]:
    command = shlex.join(
        [
            node.kernelctl,
            "fleet-receive",
            "--socket",
            node.socket,
            "--inbox",
            node.inbox,
            "--bundle-id",
            bundle_id,
            "--label",
            label,
        ]
    )
    try:
        with archive_path.open("rb") as stream:
            completed = subprocess.run(
                [*_ssh_base(node, catalog), command],
                stdin=stream,
                capture_output=True,
                check=False,
                timeout=catalog.command_timeout_s,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"fleet transport failed for {node.node_id}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace")[-2000:].strip()
        raise RuntimeError(
            f"fleet receive failed on {node.node_id} exit={completed.returncode}: {detail}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"fleet receive returned invalid JSON on {node.node_id}") from exc
    if not isinstance(response, dict) or response.get("schema") != RECEIVE_SCHEMA:
        raise RuntimeError(f"fleet receive returned invalid payload on {node.node_id}")
    return response
