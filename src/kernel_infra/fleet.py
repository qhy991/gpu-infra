"""Thin cross-host routing and immutable bundle transport."""

from __future__ import annotations

import concurrent.futures
import hashlib
import io
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
from .store import RunStore, TERMINAL_STATES, utc_now

FLEET_SCHEMA = "kernelinfra.fleet.v1"
FLEET_ENDPOINTS_SCHEMA = "kernelinfra.fleet-endpoints.v1"
BUNDLE_SCHEMA = "kernelinfra.fleet-bundle.v1"
ROUTE_SCHEMA = "kernelinfra.route-receipt.v1"
RECEIVE_SCHEMA = "kernelinfra.fleet-receive.v1"
REMOTE_OBSERVATION_SCHEMA = "kernelinfra.remote-observation.v2"
ARTIFACT_MANIFEST_SCHEMA = "kernelinfra.artifact-manifest.v1"
ARTIFACT_MIRROR_SCHEMA = "kernelinfra.artifact-mirror.v2"
FLEET_SNAPSHOT_SCHEMA = "kernelinfra.fleet-snapshot.v2"
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
MAX_ARTIFACT_FILES = 10_000
MAX_ARTIFACT_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_FLEET_SNAPSHOT_ROUTES = 256
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_SSH = re.compile(r"^[A-Za-z0-9_.@:-]+$")
_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


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


@dataclass(frozen=True)
class FleetEndpoint:
    node_id: str
    ssh_host: str
    kernelctl: str
    socket: str


@dataclass(frozen=True)
class FleetEndpoints:
    nodes: tuple[FleetEndpoint, ...]
    source_path: Path


class FleetSelectionError(RuntimeError):
    def __init__(self, message: str, decisions: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.decisions = decisions


def fleet_node(catalog: FleetCatalog, node_id: str) -> FleetNode:
    matches = [node for node in catalog.nodes if node.node_id == node_id]
    if len(matches) != 1:
        raise ContractError(f"fleet catalog has no unique node {node_id!r}")
    return matches[0]


def parse_locator(catalog: FleetCatalog, value: str) -> tuple[FleetNode, str]:
    if not isinstance(value, str) or value.count(":") != 1:
        raise ContractError("fleet locator must be node_id:run_id")
    node_id, run_id = value.split(":", 1)
    if not _ID.fullmatch(node_id) or not _RUN_ID.fullmatch(run_id):
        raise ContractError("fleet locator contains an invalid node or run id")
    return fleet_node(catalog, node_id), run_id


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


def load_fleet_endpoints(path: Path, catalog: FleetCatalog) -> FleetEndpoints:
    source = path.expanduser().resolve()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"fleet endpoint map not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid fleet endpoint map JSON at {source}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema", "nodes"}:
        raise ContractError("fleet endpoint map has invalid fields")
    if raw.get("schema") != FLEET_ENDPOINTS_SCHEMA:
        raise ContractError(
            f"fleet endpoint map schema must be {FLEET_ENDPOINTS_SCHEMA!r}"
        )
    values = raw.get("nodes")
    if not isinstance(values, list) or not values:
        raise ContractError("fleet endpoint map nodes must be a non-empty list")
    catalog_ids = {node.node_id for node in catalog.nodes}
    nodes: list[FleetEndpoint] = []
    for index, value in enumerate(values):
        where = f"fleet_endpoints.nodes[{index}]"
        if not isinstance(value, dict) or set(value) != {
            "id",
            "ssh",
            "kernelctl",
            "socket",
        }:
            raise ContractError(f"{where} has invalid fields")
        node_id = value["id"]
        if not isinstance(node_id, str) or not _ID.fullmatch(node_id):
            raise ContractError(f"{where}.id is invalid")
        if node_id not in catalog_ids:
            raise ContractError(
                f"{where}.id {node_id!r} is absent from the historical catalog"
            )
        ssh_host = value["ssh"]
        if not isinstance(ssh_host, str) or not _SSH.fullmatch(ssh_host):
            raise ContractError(f"{where}.ssh is unsafe")
        nodes.append(
            FleetEndpoint(
                node_id=node_id,
                ssh_host=ssh_host,
                kernelctl=_safe_remote_path(
                    value["kernelctl"], f"{where}.kernelctl"
                ),
                socket=_safe_remote_path(value["socket"], f"{where}.socket"),
            )
        )
    if len({node.node_id for node in nodes}) != len(nodes):
        raise ContractError("fleet endpoint map node ids must be unique")
    return FleetEndpoints(nodes=tuple(nodes), source_path=source)


def resolve_fleet_endpoint(
    *,
    catalog: FleetCatalog,
    node: FleetNode,
    endpoints: FleetEndpoints | None,
) -> tuple[FleetNode, dict[str, Any]]:
    if endpoints is None:
        return node, _endpoint_record(
            owner="historical-catalog", source=catalog.source_path, node=node
        )
    matches = [item for item in endpoints.nodes if item.node_id == node.node_id]
    if len(matches) != 1:
        raise ContractError(
            f"fleet endpoint map has no unique endpoint for {node.node_id!r}"
        )
    endpoint = matches[0]
    effective = FleetNode(
        node_id=node.node_id,
        ssh_host=endpoint.ssh_host,
        kernelctl=endpoint.kernelctl,
        socket=endpoint.socket,
        inbox=node.inbox,
        capabilities=node.capabilities,
    )
    return effective, _endpoint_record(
        owner="fleet-endpoints", source=endpoints.source_path, node=effective
    )


def _endpoint_record(
    *, owner: str, source: Path, node: FleetNode
) -> dict[str, Any]:
    return {
        "owner": owner,
        "source": str(source),
        "node_id": node.node_id,
        "ssh": node.ssh_host,
        "kernelctl": node.kernelctl,
        "socket": node.socket,
    }


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


def remote_kernelctl_json(
    *,
    node: FleetNode,
    catalog: FleetCatalog,
    arguments: list[str],
    timeout_s: float | None = None,
    expect_json: bool = True,
    allowed_exit_codes: frozenset[int] = frozenset({0}),
) -> Any:
    command = shlex.join([node.kernelctl, *arguments])
    limit = catalog.command_timeout_s if timeout_s is None else timeout_s
    try:
        completed = subprocess.run(
            [*_ssh_base(node, catalog), command],
            capture_output=True,
            text=True,
            check=False,
            timeout=limit,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"remote operation failed for {node.node_id}: {exc}"
        ) from exc
    if completed.returncode not in allowed_exit_codes:
        raise RuntimeError(
            f"remote operation failed on {node.node_id} exit={completed.returncode}: "
            f"{completed.stderr[-2000:].strip()}"
        )
    if not expect_json:
        return {"exit_code": completed.returncode}
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"remote operation returned invalid JSON on {node.node_id}"
        ) from exc


def load_route_receipt(path: Path, catalog: FleetCatalog) -> dict[str, Any]:
    source = path.expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid route receipt {source}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != ROUTE_SCHEMA:
        raise ContractError("invalid route receipt schema")
    content = dict(value)
    claimed = content.pop("route_receipt_sha256", None)
    if not isinstance(claimed, str) or digest_json(content) != claimed:
        raise ContractError("route receipt digest disagrees with content")
    if value.get("status") != "submitted" or not isinstance(
        value.get("locator"), dict
    ):
        raise ContractError("route receipt does not own a submitted locator")
    if value.get("catalog_sha256") != catalog.digest:
        raise ContractError("route receipt catalog digest drift")
    node_id = value["locator"].get("node_id")
    run_id = value["locator"].get("run_id")
    node, parsed_run = parse_locator(catalog, f"{node_id}:{run_id}")
    if node.node_id != value.get("selected_node") or parsed_run != run_id:
        raise ContractError("route receipt locator disagrees with selected node")
    remote = value.get("remote")
    if not isinstance(remote, dict):
        raise ContractError("route receipt has no remote receive payload")
    bundle_id = value.get("bundle_id")
    expected_dir = f"{node.inbox.rstrip('/')}/{bundle_id}"
    if remote.get("bundle_id") != bundle_id or remote.get("bundle_dir") != expected_dir:
        raise ContractError("route receipt remote bundle path disagrees with catalog")
    if not isinstance(remote.get("run"), dict) or remote["run"].get("run_id") != run_id:
        raise ContractError("route receipt remote run disagrees with locator")
    return value


def route_locator_from_receipt(
    path: Path, catalog: FleetCatalog
) -> tuple[FleetNode, str, dict[str, Any]]:
    receipt = load_route_receipt(path, catalog)
    locator = receipt["locator"]
    node, run_id = parse_locator(
        catalog, f"{locator['node_id']}:{locator['run_id']}"
    )
    return node, run_id, receipt


def validate_route_run_response(
    *, response: Any, route: dict[str, Any], run_id: str, operation: str
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise RuntimeError(f"remote {operation} returned a non-object")
    for field, expected in (
        ("run_id", run_id),
        ("task_id", route["task_id"]),
        ("task_sha256", route["task_sha256"]),
        ("candidate_sha256", route["candidate_sha256"]),
    ):
        if response.get(field) != expected:
            raise RuntimeError(f"remote {operation} {field} drift")
    expected_run_dir = route["remote"]["run"].get("run_dir")
    if expected_run_dir is not None and response.get("run_dir") != expected_run_dir:
        raise RuntimeError(f"remote {operation} run_dir drift")
    if not isinstance(response.get("state"), str) or not response["state"]:
        raise RuntimeError(f"remote {operation} has no lifecycle state")
    return response


def query_route_status(
    *,
    node: FleetNode,
    catalog: FleetCatalog,
    run_id: str,
    route: dict[str, Any],
    query: Callable[..., Any] = remote_kernelctl_json,
) -> dict[str, Any]:
    value = query(
        node=node,
        catalog=catalog,
        arguments=["status", "--socket", node.socket, "--json", run_id],
    )
    if not isinstance(value, list) or len(value) != 1:
        raise RuntimeError("remote status did not return one run")
    return validate_route_run_response(
        response=value[0], route=route, run_id=run_id, operation="status"
    )


def remote_observation_receipt(
    *,
    catalog: FleetCatalog,
    node: FleetNode,
    run_id: str,
    operation: str,
    response: Any,
    error: str | None,
    endpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if endpoint is None:
        endpoint = _endpoint_record(
            owner="historical-catalog", source=catalog.source_path, node=node
        )
    value = {
        "schema": REMOTE_OBSERVATION_SCHEMA,
        "observed_at": utc_now(),
        "catalog": str(catalog.source_path),
        "catalog_sha256": catalog.digest,
        "operation": operation,
        "locator": {"node_id": node.node_id, "run_id": run_id},
        "endpoint": endpoint,
        "status": "ok" if error is None else "unknown",
        "response": response,
        "error": error,
    }
    return {**value, "observation_sha256": digest_json(value)}


def fleet_snapshot(
    *,
    catalog: FleetCatalog,
    route_paths: list[Path],
    endpoints: FleetEndpoints | None = None,
    query: Callable[..., Any] = remote_kernelctl_json,
) -> dict[str, Any]:
    if not route_paths:
        raise ContractError("fleet snapshot requires at least one route receipt")
    if len(route_paths) > MAX_FLEET_SNAPSHOT_ROUTES:
        raise ContractError(
            f"fleet snapshot supports at most {MAX_FLEET_SNAPSHOT_ROUTES} routes"
        )

    targets: list[
        tuple[Path, FleetNode, str, dict[str, Any], dict[str, Any]]
    ] = []
    locators: set[tuple[str, str]] = set()
    # Validate every route before opening any SSH connection. A malformed batch
    # cannot yield a partial view that silently omits an invalid input.
    for path in route_paths:
        source = path.expanduser().resolve()
        historical_node, run_id, route = route_locator_from_receipt(source, catalog)
        node, endpoint = resolve_fleet_endpoint(
            catalog=catalog, node=historical_node, endpoints=endpoints
        )
        locator = (historical_node.node_id, run_id)
        if locator in locators:
            raise ContractError(
                f"fleet snapshot contains duplicate locator {node.node_id}:{run_id}"
            )
        locators.add(locator)
        targets.append((source, node, run_id, route, endpoint))
    targets.sort(key=lambda item: (item[1].node_id, item[2]))

    def observe(
        target: tuple[
            Path, FleetNode, str, dict[str, Any], dict[str, Any]
        ]
    ) -> dict[str, Any]:
        source, node, run_id, route, endpoint = target
        response: dict[str, Any] | None = None
        error: str | None = None
        try:
            response = query_route_status(
                node=node,
                catalog=catalog,
                run_id=run_id,
                route=route,
                query=query,
            )
        except Exception as exc:
            response = None
            error = f"{type(exc).__name__}: {exc}"
        return {
            "route": str(source),
            "route_receipt_sha256": route["route_receipt_sha256"],
            "locator": {"node_id": node.node_id, "run_id": run_id},
            "endpoint": endpoint,
            "status": "ok" if error is None else "unknown",
            "response": response,
            "error": error,
        }

    observations: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(16, len(targets))
    ) as executor:
        futures = [executor.submit(observe, target) for target in targets]
        observations = [future.result() for future in futures]
    observations.sort(
        key=lambda item: (item["locator"]["node_id"], item["locator"]["run_id"])
    )

    states: dict[str, int] = {}
    terminal = 0
    nonterminal = 0
    ok = 0
    for observation in observations:
        if observation["status"] != "ok":
            continue
        ok += 1
        state = observation["response"]["state"]
        states[state] = states.get(state, 0) + 1
        if state in TERMINAL_STATES:
            terminal += 1
        else:
            nonterminal += 1
    return {
        "schema": FLEET_SNAPSHOT_SCHEMA,
        "observed_at": utc_now(),
        "catalog": str(catalog.source_path),
        "observations": observations,
        "summary": {
            "total": len(observations),
            "ok": ok,
            "unknown": len(observations) - ok,
            "terminal": terminal,
            "nonterminal": nonterminal,
            "states": dict(sorted(states.items())),
        },
    }


def _artifact_set_sha256(files: list[tuple[Path, str]]) -> str:
    """One transfer-boundary digest, not one fingerprint per artifact."""
    digest = hashlib.sha256()
    for path, relative in sorted(files, key=lambda item: item[1]):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _safe_artifact_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RuntimeError(f"unsafe artifact path: {name!r}")
    return path


def _safe_artifact_archive_path(name: str) -> tuple[str, PurePosixPath | None]:
    path = _safe_artifact_relative(name)
    if path.as_posix() == "artifact-manifest.json":
        return "manifest", None
    if path.parts[0] != "artifacts" or len(path.parts) < 2:
        raise RuntimeError(f"unexpected artifact archive path: {name!r}")
    return "artifact", PurePosixPath(*path.parts[1:])


def _validate_artifact_manifest(
    value: Any, *, max_bytes: int
) -> dict[str, Any]:
    fields = {
        "schema",
        "authority",
        "terminal_at",
        "run_id",
        "task_id",
        "task_sha256",
        "candidate_sha256",
        "terminal_state",
        "run_dir",
        "total_bytes",
        "files",
        "artifact_set_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeError("artifact manifest has invalid fields")
    if (
        value.get("schema") != ARTIFACT_MANIFEST_SCHEMA
        or value.get("authority") != "node-run"
    ):
        raise RuntimeError("artifact manifest authority drift")
    if not isinstance(value.get("artifact_set_sha256"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["artifact_set_sha256"]
    ):
        raise RuntimeError("artifact manifest set identity is invalid")
    if (
        not isinstance(value.get("run_id"), str)
        or not _RUN_ID.fullmatch(value["run_id"])
        or not isinstance(value.get("task_id"), str)
        or not _ID.fullmatch(value["task_id"])
    ):
        raise RuntimeError("artifact manifest has invalid run/task identity")
    for name in ("task_sha256", "candidate_sha256"):
        if not isinstance(value.get(name), str) or not re.fullmatch(
            r"[0-9a-f]{64}", value[name]
        ):
            raise RuntimeError(f"artifact manifest has invalid {name}")
    if value.get("terminal_state") not in {
        "completed",
        "rejected",
        "infra_error",
        "cancelled",
        "interrupted",
    }:
        raise RuntimeError("artifact manifest is not terminal")
    if not isinstance(value.get("terminal_at"), str) or not value["terminal_at"]:
        raise RuntimeError("artifact manifest has no terminal timestamp")
    if not isinstance(value.get("run_dir"), str) or not Path(
        value["run_dir"]
    ).is_absolute():
        raise RuntimeError("artifact manifest run directory is invalid")
    total_bytes = value.get("total_bytes")
    if (
        isinstance(total_bytes, bool)
        or not isinstance(total_bytes, int)
        or total_bytes < 0
        or total_bytes > max_bytes
    ):
        raise RuntimeError("artifact manifest byte total is invalid")
    rows = value.get("files")
    if not isinstance(rows, list) or len(rows) > MAX_ARTIFACT_FILES:
        raise RuntimeError("artifact manifest file list is invalid")
    paths: list[str] = []
    calculated_total = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "size"}:
            raise RuntimeError("artifact manifest file row is invalid")
        if not isinstance(row.get("path"), str):
            raise RuntimeError("artifact manifest file path is invalid")
        normalized = _safe_artifact_relative(row["path"]).as_posix()
        if normalized != row["path"]:
            raise RuntimeError("artifact manifest file path is not normalized")
        size = row.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RuntimeError("artifact manifest file size is invalid")
        paths.append(normalized)
        calculated_total += size
    if paths != sorted(set(paths)):
        raise RuntimeError("artifact manifest paths must be sorted and unique")
    if calculated_total != total_bytes:
        raise RuntimeError("artifact manifest byte total disagrees with files")
    return value


def build_artifact_manifest(
    *, run_state: dict[str, Any], max_bytes: int
) -> tuple[dict[str, Any], list[tuple[Path, str]]]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise RuntimeError("artifact export byte limit must be positive")
    if run_state.get("state") not in {
        "completed",
        "rejected",
        "infra_error",
        "cancelled",
        "interrupted",
    }:
        raise RuntimeError("fleet artifact export requires a terminal run")
    source_run_dir = Path(str(run_state.get("run_dir", ""))).expanduser()
    if source_run_dir.is_symlink():
        raise RuntimeError(f"run artifact directory is a symlink: {source_run_dir}")
    run_dir = source_run_dir.resolve()
    if not run_dir.is_dir():
        raise RuntimeError(f"run artifact directory is missing: {run_dir}")
    files: list[tuple[Path, str]] = []
    total = 0
    for root_text, directories, names in os.walk(run_dir, followlinks=False):
        root = Path(root_text)
        for directory in directories:
            if (root / directory).is_symlink():
                raise RuntimeError(f"run artifacts contain symlink: {root / directory}")
        for name in sorted(names):
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"run artifacts contain non-file: {path}")
            relative = path.relative_to(run_dir).as_posix()
            size = path.stat().st_size
            total += size
            if total > max_bytes:
                raise RuntimeError("run artifacts exceed export byte limit")
            files.append((path, relative))
            if len(files) > MAX_ARTIFACT_FILES:
                raise RuntimeError("run artifacts exceed export file limit")
    rows = [
        {
            "path": relative,
            "size": path.stat().st_size,
        }
        for path, relative in sorted(files, key=lambda item: item[1])
    ]
    value = {
        "schema": ARTIFACT_MANIFEST_SCHEMA,
        "authority": "node-run",
        "terminal_at": run_state.get("terminal_at"),
        "run_id": run_state["run_id"],
        "task_id": run_state["task_id"],
        "task_sha256": run_state["task_sha256"],
        "candidate_sha256": run_state["candidate_sha256"],
        "terminal_state": run_state["state"],
        "run_dir": str(run_dir),
        "total_bytes": total,
        "files": rows,
        "artifact_set_sha256": _artifact_set_sha256(files),
    }
    return value, files


def write_artifact_export(
    *, run_state: dict[str, Any], stream: BinaryIO, max_bytes: int
) -> dict[str, Any]:
    manifest, files = build_artifact_manifest(
        run_state=run_state, max_bytes=max_bytes
    )
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    with tarfile.open(fileobj=stream, mode="w|") as archive:
        info = tarfile.TarInfo("artifact-manifest.json")
        info.size = len(encoded)
        info.mode = 0o600
        info.mtime = 0
        archive.addfile(info, io.BytesIO(encoded))
        for path, relative in files:
            info = tarfile.TarInfo("artifacts/" + relative)
            info.size = path.stat().st_size
            info.mode = 0o600
            info.mtime = 0
            with path.open("rb") as source:
                archive.addfile(info, source)
    return manifest


def receive_artifact_export(
    *, stream: BinaryIO, workspace: Path, max_bytes: int
) -> dict[str, Any]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise RuntimeError("artifact receive byte limit must be positive")
    root = workspace.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(root.iterdir()):
        raise RuntimeError("artifact receive workspace must be empty")
    seen: set[str] = set()
    artifact_bytes = 0
    member_count = 0
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            member_count += 1
            if member_count > MAX_ARTIFACT_FILES + 1:
                raise RuntimeError("artifact archive exceeds member limit")
            kind, relative = _safe_artifact_archive_path(member.name)
            normalized = member.name
            if normalized in seen:
                raise RuntimeError(f"duplicate artifact archive path: {normalized}")
            seen.add(normalized)
            if not member.isfile():
                raise RuntimeError(
                    f"artifact archive contains non-file: {normalized}"
                )
            if kind == "manifest":
                if member_count != 1:
                    raise RuntimeError("artifact manifest must be the first member")
                if member.size > MAX_ARTIFACT_MANIFEST_BYTES:
                    raise RuntimeError("artifact manifest exceeds byte limit")
                target = root / "artifact-manifest.json"
            else:
                assert relative is not None
                artifact_bytes += member.size
                if artifact_bytes > max_bytes:
                    raise RuntimeError("artifact archive exceeds extracted byte limit")
                target = root / "artifacts" / Path(*relative.parts)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"cannot read artifact archive member: {normalized}")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o600)
    try:
        manifest = json.loads(
            (root / "artifact-manifest.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid artifact manifest: {exc}") from exc
    manifest = _validate_artifact_manifest(manifest, max_bytes=max_bytes)
    expected = {row["path"]: row for row in manifest["files"]}
    observed: dict[str, Path] = {}
    artifacts = root / "artifacts"
    if artifacts.exists():
        for path in artifacts.rglob("*"):
            if path.is_symlink():
                raise RuntimeError(f"artifact mirror contains symlink: {path}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise RuntimeError(f"artifact mirror contains non-file: {path}")
            relative = path.relative_to(artifacts).as_posix()
            observed[relative] = path
    if set(observed) != set(expected):
        raise RuntimeError("artifact archive file set disagrees with manifest")
    for relative, path in observed.items():
        row = expected[relative]
        if path.stat().st_size != row["size"]:
            raise RuntimeError(f"artifact archive size drift: {relative}")
    if artifact_bytes != manifest["total_bytes"]:
        raise RuntimeError("artifact archive byte total disagrees with manifest")
    artifact_files = [(path, relative) for relative, path in observed.items()]
    if _artifact_set_sha256(artifact_files) != manifest["artifact_set_sha256"]:
        raise RuntimeError("artifact archive content drift")
    return manifest


def install_artifact_mirror(
    *,
    stream: BinaryIO,
    destination: Path,
    catalog: FleetCatalog,
    route: dict[str, Any],
    max_bytes: int,
    endpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output = destination.expanduser().resolve()
    if output.exists():
        raise RuntimeError(f"refusing to overwrite artifact mirror: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.incoming-", dir=output.parent))
    installed = False
    try:
        manifest = receive_artifact_export(
            stream=stream, workspace=staging, max_bytes=max_bytes
        )
        locator = route["locator"]
        identity_pairs = (
            ("run_id", locator["run_id"]),
            ("task_id", route["task_id"]),
            ("task_sha256", route["task_sha256"]),
            ("candidate_sha256", route["candidate_sha256"]),
        )
        for field, expected in identity_pairs:
            if manifest.get(field) != expected:
                raise RuntimeError(f"artifact manifest {field} disagrees with route")
        expected_run_dir = route["remote"]["run"].get("run_dir")
        if expected_run_dir is not None and manifest.get("run_dir") != expected_run_dir:
            raise RuntimeError("artifact manifest run_dir disagrees with route")
        if endpoint is None:
            historical_node = fleet_node(catalog, locator["node_id"])
            _node, endpoint = resolve_fleet_endpoint(
                catalog=catalog, node=historical_node, endpoints=None
            )
        catalog_raw = json.loads(catalog.source_path.read_text(encoding="utf-8"))
        if digest_json(catalog_raw) != catalog.digest:
            raise RuntimeError("fleet catalog changed during artifact fetch")
        RunStore.atomic_json(staging / "catalog.json", catalog_raw, mode=0o600)
        RunStore.atomic_json(staging / "route.json", route, mode=0o600)
        RunStore.atomic_json(staging / "endpoint.json", endpoint, mode=0o600)
        value = {
            "schema": ARTIFACT_MIRROR_SCHEMA,
            "authority": "mirror-only",
            "mirrored_at": utc_now(),
            "route_receipt_sha256": route["route_receipt_sha256"],
            "locator": {
                "node_id": locator["node_id"],
                "run_id": locator["run_id"],
            },
            "transport_endpoint": "endpoint.json",
            "artifact_manifest": "artifact-manifest.json",
            "artifact_root": "artifacts",
            "validation": "passed",
        }
        mirror = value
        RunStore.atomic_json(staging / "mirror.json", mirror, mode=0o600)
        if output.exists():
            raise RuntimeError(f"refusing to overwrite artifact mirror: {output}")
        os.rename(staging, output)
        installed = True
        return mirror
    finally:
        if not installed and staging.exists():
            shutil.rmtree(staging)


def fetch_artifact_export(
    *,
    node: FleetNode,
    catalog: FleetCatalog,
    run_id: str,
    archive_path: Path,
    max_bytes: int,
    timeout_s: float | None = None,
) -> None:
    command = shlex.join(
        [
            node.kernelctl,
            "fleet-export",
            "--socket",
            node.socket,
            "--max-bytes",
            str(max_bytes),
            run_id,
        ]
    )
    limit = catalog.command_timeout_s if timeout_s is None else timeout_s
    try:
        with archive_path.open("xb") as output:
            completed = subprocess.run(
                [*_ssh_base(node, catalog), command],
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
                timeout=limit,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"artifact fetch failed for {node.node_id}: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace")[-2000:].strip()
        raise RuntimeError(
            f"artifact export failed on {node.node_id} "
            f"exit={completed.returncode}: {detail}"
        )
