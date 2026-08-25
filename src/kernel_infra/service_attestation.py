"""Attest that an evaluator service is held by a live broker allocation."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "kernelinfra.service-deployment.v2"
BROKER_ADMISSION_SCHEMA = "gpuq.admission-receipt.v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _broker_request(
    socket_path: Path, request: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, int | None]]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(10.0)
        client.connect(str(socket_path.expanduser().resolve()))
        peer = {"pid": None, "uid": None, "gid": None}
        if hasattr(socket, "SO_PEERCRED"):
            raw_peer = client.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            peer["pid"], peer["uid"], peer["gid"] = struct.unpack("3i", raw_peer)
        connection = client.makefile("rwb")
        with client, connection:
            connection.write(
                (json.dumps(request, separators=(",", ":")) + "\n").encode()
            )
            connection.flush()
            value = json.loads(connection.readline())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot query broker at {socket_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("broker returned a non-object payload")
    return value, peer


def query_broker(socket_path: Path) -> dict[str, Any]:
    value, peer = _broker_request(socket_path, {"op": "status"})
    snapshot = value.get("snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError("broker returned an invalid status payload")
    return {
        **snapshot,
        "_kernelinfra_peer_pid": peer["pid"],
        "_kernelinfra_peer_uid": peer["uid"],
        "_kernelinfra_peer_gid": peer["gid"],
    }


def query_broker_admission(
    socket_path: Path, job_id: str
) -> tuple[dict[str, Any], dict[str, int | None]]:
    value, peer = _broker_request(
        socket_path, {"op": "receipt", "job_id": job_id}
    )
    receipt = value.get("receipt")
    if value.get("ok") is not True or not isinstance(receipt, dict):
        raise RuntimeError(f"broker has no live admission receipt for {job_id}")
    return validate_broker_admission_receipt(receipt), peer


def request_json(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read())
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot query service endpoint {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"service endpoint returned a non-object: {url}")
    return value


def _normalize_loopback_url(value: str) -> str:
    url = value.rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("v0 service deployment must use a loopback HTTP URL")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("service URL must contain only scheme, loopback host, and port")
    if parsed.port is None:
        raise ValueError("service URL must include an explicit port")
    return url


def _running_job(snapshot: dict[str, Any], job_id: str) -> dict[str, Any]:
    running = snapshot.get("running")
    if not isinstance(running, list):
        raise RuntimeError("broker snapshot has no running list")
    matches = [job for job in running if job.get("job_id") == job_id]
    if len(matches) != 1:
        raise RuntimeError(f"broker job is not uniquely running: {job_id}")
    job = matches[0]
    if job.get("mode") != "exclusive":
        raise RuntimeError("evaluator service must hold an exclusive broker allocation")
    gpu_ids = job.get("gpu_ids")
    if not isinstance(gpu_ids, list) or not gpu_ids:
        raise RuntimeError("evaluator service has no broker GPU allocation")
    return job


def _service_documents(service_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    health = request_json(service_url + "/health")
    if health.get("status") != "ok":
        raise RuntimeError(f"service health is not ok: {health!r}")
    workers = health.get("workers")
    if not isinstance(workers, list) or not workers or not all(
        isinstance(worker, dict) and worker.get("healthy") is True
        for worker in workers
    ):
        raise RuntimeError(f"service has no fully healthy worker set: {health!r}")
    root = request_json(service_url + "/")
    return health, root


def _broker_version(snapshot: dict[str, Any]) -> Any:
    return snapshot.get("broker_version", snapshot.get("version"))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise RuntimeError(f"{where} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise RuntimeError(f"{where} must be a SHA-256 hex digest") from exc
    return value


def validate_broker_admission_receipt(value: Any) -> dict[str, Any]:
    required = {
        "schema",
        "job_id",
        "broker_version",
        "broker_instance_id",
        "submitted_at",
        "started_at",
        "owner",
        "label",
        "mode",
        "gpu_count",
        "gpu_ids",
        "cwd",
        "argv_count",
        "argv_sha256",
        "env_keys",
        "env_sha256",
        "launch_spec_sha256",
        "resolved_executable",
        "executable_sha256",
        "effective_env_sha256",
        "receipt_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema") != BROKER_ADMISSION_SCHEMA
    ):
        raise RuntimeError("invalid broker admission receipt")
    if value.get("mode") != "exclusive":
        raise RuntimeError("service broker admission must be exclusive")
    for key in (
        "job_id",
        "broker_version",
        "broker_instance_id",
        "submitted_at",
        "started_at",
        "owner",
        "label",
        "cwd",
        "resolved_executable",
    ):
        if not isinstance(value.get(key), str) or not value[key]:
            raise RuntimeError(f"broker admission {key} must be non-empty text")
    for key in ("gpu_count", "argv_count"):
        if (
            not isinstance(value.get(key), int)
            or isinstance(value[key], bool)
            or value[key] < 1
        ):
            raise RuntimeError(f"broker admission {key} must be positive")
    if not Path(value["cwd"]).is_absolute() or not Path(
        value["resolved_executable"]
    ).is_absolute():
        raise RuntimeError("broker admission paths must be absolute")
    gpu_ids = value.get("gpu_ids")
    if not isinstance(gpu_ids, list) or not gpu_ids or not all(
        isinstance(gpu_id, int) and not isinstance(gpu_id, bool) and gpu_id >= 0
        for gpu_id in gpu_ids
    ):
        raise RuntimeError("broker admission receipt has invalid GPU ids")
    if value.get("gpu_count") != len(gpu_ids):
        raise RuntimeError("broker admission GPU count disagrees with allocation")
    if not isinstance(value.get("started_at"), str) or not value["started_at"]:
        raise RuntimeError("broker admission receipt is not a started job")
    if not isinstance(value.get("env_keys"), list) or not all(
        isinstance(key, str) for key in value["env_keys"]
    ):
        raise RuntimeError("broker admission receipt has invalid environment keys")
    if value["env_keys"] != sorted(set(value["env_keys"])):
        raise RuntimeError("broker admission environment keys must be unique and sorted")
    for key in (
        "argv_sha256",
        "env_sha256",
        "launch_spec_sha256",
        "executable_sha256",
        "effective_env_sha256",
        "receipt_sha256",
    ):
        _sha256_text(value.get(key), f"broker admission {key}")
    content = dict(value)
    claimed = content.pop("receipt_sha256")
    if _digest_json(content) != claimed:
        raise RuntimeError("broker admission receipt digest disagrees with content")
    return dict(value)


def load_broker_admission_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load broker admission receipt {path}: {exc}") from exc
    return validate_broker_admission_receipt(value)


def _git_source_identity(source_root: Path) -> dict[str, str | bool]:
    root = source_root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"service source root is not a directory: {root}")

    def git(*arguments: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), *arguments],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"cannot attest service source at {root}: {exc}") from exc
        return completed.stdout.strip()

    commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    dirty = bool(git("status", "--porcelain=v1", "--untracked-files=all"))
    if dirty:
        raise RuntimeError(f"service source checkout is dirty: {root}")
    return {
        "source_root": str(root),
        "source_commit": commit,
        "source_tree": tree,
        "source_dirty": False,
    }


def _validate_admission_against_live_job(
    *,
    admission: dict[str, Any],
    snapshot: dict[str, Any],
    job: dict[str, Any],
    peer: dict[str, int | None],
) -> None:
    if admission["broker_version"] != _broker_version(snapshot):
        raise RuntimeError("broker admission version disagrees with live broker")
    if admission["broker_instance_id"] != snapshot.get("instance_id"):
        raise RuntimeError("broker admission instance disagrees with live broker")
    for key in ("pid", "uid", "gid"):
        if peer[key] != snapshot.get(f"_kernelinfra_peer_{key}"):
            raise RuntimeError(f"broker admission peer disagrees with status peer: {key}")
    for admission_key, job_key in (
        ("job_id", "job_id"),
        ("submitted_at", "submitted_at"),
        ("started_at", "started_at"),
        ("owner", "owner"),
        ("label", "label"),
        ("mode", "mode"),
        ("gpu_count", "gpu_count"),
        ("gpu_ids", "gpu_ids"),
    ):
        if admission[admission_key] != job.get(job_key):
            raise RuntimeError(
                f"broker admission disagrees with live job: {admission_key}"
            )


def build_service_receipt(
    *,
    broker_socket: Path,
    broker_job_id: str,
    broker_admission_receipt: Path,
    service_url: str,
    service_identity: str,
    source_root: Path,
) -> dict[str, Any]:
    if not broker_job_id:
        raise ValueError("broker job id is required")
    if not service_identity:
        raise ValueError("service identity is required")
    url = _normalize_loopback_url(service_url)
    snapshot = query_broker(broker_socket)
    job = _running_job(snapshot, broker_job_id)
    admission = load_broker_admission_receipt(
        broker_admission_receipt.expanduser().resolve()
    )
    live_admission, admission_peer = query_broker_admission(
        broker_socket, broker_job_id
    )
    if live_admission != admission:
        raise RuntimeError("saved broker admission receipt differs from live broker")
    _validate_admission_against_live_job(
        admission=admission,
        snapshot=snapshot,
        job=job,
        peer=admission_peer,
    )
    for key in ("launch_spec_sha256", "executable_sha256"):
        if str(admission[key]) not in service_identity:
            raise RuntimeError(f"service identity does not bind broker admission {key}")
    health, root = _service_documents(url)
    source = _git_source_identity(source_root)
    if str(source["source_commit"]) not in service_identity:
        raise RuntimeError("service identity does not bind the attested source commit")
    commit = root.get("commit")
    if isinstance(commit, str) and commit and commit not in service_identity:
        raise RuntimeError("service identity does not bind the reported service commit")
    return {
        "schema": SCHEMA,
        "attested_at": _utc_now(),
        "service_url": url,
        "service_identity": service_identity,
        "service_root": root,
        "service_health": health,
        "broker_admission_receipt": admission,
        **source,
        "broker_socket": str(broker_socket.expanduser().resolve()),
        "broker_version": _broker_version(snapshot),
        "broker_instance_id": snapshot.get("instance_id"),
        "broker_peer_pid": snapshot.get("_kernelinfra_peer_pid"),
        "broker_peer_uid": snapshot.get("_kernelinfra_peer_uid"),
        "broker_peer_gid": snapshot.get("_kernelinfra_peer_gid"),
        "broker_job_id": broker_job_id,
        "broker_job_submitted_at": job.get("submitted_at"),
        "broker_job_started_at": job.get("started_at"),
        "broker_label": job.get("label"),
        "broker_owner": job.get("owner"),
        "broker_mode": job.get("mode"),
        "broker_gpu_ids": job.get("gpu_ids"),
    }


def load_service_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load deployment receipt {path}: {exc}") from exc
    required = {
        "schema",
        "attested_at",
        "service_url",
        "service_identity",
        "service_root",
        "service_health",
        "broker_admission_receipt",
        "source_root",
        "source_commit",
        "source_tree",
        "source_dirty",
        "broker_socket",
        "broker_version",
        "broker_instance_id",
        "broker_peer_pid",
        "broker_peer_uid",
        "broker_peer_gid",
        "broker_job_id",
        "broker_job_submitted_at",
        "broker_job_started_at",
        "broker_label",
        "broker_owner",
        "broker_mode",
        "broker_gpu_ids",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("schema") != SCHEMA:
        raise RuntimeError("invalid service deployment receipt")
    if value.get("source_dirty") is not False:
        raise RuntimeError("service deployment receipt has a dirty source checkout")
    value["broker_admission_receipt"] = validate_broker_admission_receipt(
        value["broker_admission_receipt"]
    )
    _normalize_loopback_url(str(value["service_url"]))
    return value


def verify_service_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    source = _git_source_identity(Path(str(receipt["source_root"])))
    for key in ("source_root", "source_commit", "source_tree", "source_dirty"):
        if source[key] != receipt[key]:
            raise RuntimeError(f"service source identity changed after attestation: {key}")
    snapshot = query_broker(Path(str(receipt["broker_socket"])))
    if snapshot.get("instance_id") != receipt["broker_instance_id"]:
        raise RuntimeError("broker instance changed after deployment attestation")
    if _broker_version(snapshot) != receipt["broker_version"]:
        raise RuntimeError("broker version changed after deployment attestation")
    for key in ("pid", "uid", "gid"):
        if snapshot.get(f"_kernelinfra_peer_{key}") != receipt[f"broker_peer_{key}"]:
            raise RuntimeError(f"broker peer changed after deployment attestation: {key}")
    job = _running_job(snapshot, str(receipt["broker_job_id"]))
    live_admission, admission_peer = query_broker_admission(
        Path(str(receipt["broker_socket"])), str(receipt["broker_job_id"])
    )
    if live_admission != receipt["broker_admission_receipt"]:
        raise RuntimeError("broker admission receipt changed after attestation")
    _validate_admission_against_live_job(
        admission=live_admission,
        snapshot=snapshot,
        job=job,
        peer=admission_peer,
    )
    for receipt_key, job_key in (
        ("broker_job_submitted_at", "submitted_at"),
        ("broker_job_started_at", "started_at"),
        ("broker_label", "label"),
        ("broker_owner", "owner"),
        ("broker_mode", "mode"),
        ("broker_gpu_ids", "gpu_ids"),
    ):
        if receipt[receipt_key] != job.get(job_key):
            raise RuntimeError(f"broker deployment field changed: {receipt_key}")
    health, root = _service_documents(str(receipt["service_url"]))
    if root != receipt["service_root"]:
        raise RuntimeError("service root identity changed after attestation")
    return {
        "broker": snapshot,
        "job": job,
        "admission": live_admission,
        "health": health,
        "root": root,
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
