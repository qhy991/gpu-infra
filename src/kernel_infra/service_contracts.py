"""Strict managed evaluator-service launch contracts."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import ContractError, digest_json
from .service_attestation import _normalize_loopback_url

SERVICE_SCHEMA = "kernelinfra.service.v1"
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")


@dataclass(frozen=True)
class ServiceResources:
    gpu_count: int
    estimate_s: float | None
    queue_timeout_s: float | None
    run_timeout_s: float


@dataclass(frozen=True)
class ManagedServiceSpec:
    service_id: str
    description: str
    owner: str
    service_url: str
    source_root: Path
    identity_prefix: str
    cwd: Path
    command: tuple[str, ...]
    env: dict[str, str]
    resources: ServiceResources
    readiness_timeout_s: float
    idle_grace_s: float | None
    raw: dict[str, Any]
    digest: str
    source_path: Path


def _strict_keys(
    value: dict[str, Any], *, required: set[str], optional: set[str], where: str
) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required - optional)
    if missing:
        raise ContractError(f"{where} missing fields: {', '.join(missing)}")
    if unknown:
        raise ContractError(f"{where} unknown fields: {', '.join(unknown)}")


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{where} must be an object")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{where} must be non-empty text")
    return value.strip()


def _positive(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{where} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ContractError(f"{where} must be a finite positive number")
    return result


def _resolved_path(value: Any, where: str, base: Path) -> Path:
    text = _text(value, where)
    path = Path(text).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def load_service_spec(path: Path) -> ManagedServiceSpec:
    source = path.expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"service spec not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid service spec JSON at {source}: {exc}") from exc
    raw = _object(value, "service")
    _strict_keys(
        raw,
        required={
            "schema",
            "service_id",
            "owner",
            "service_url",
            "source_root",
            "identity_prefix",
            "launch",
            "resources",
        },
        optional={"description", "readiness_timeout_s", "idle_grace_s"},
        where="service",
    )
    if raw["schema"] != SERVICE_SCHEMA:
        raise ContractError(f"service.schema must be {SERVICE_SCHEMA!r}")
    service_id = _text(raw["service_id"], "service.service_id")
    if not _ID.fullmatch(service_id):
        raise ContractError(f"service.service_id must match {_ID.pattern!r}")
    owner = _text(raw["owner"], "service.owner")
    identity_prefix = _text(raw["identity_prefix"], "service.identity_prefix")
    if "admission@sha256:" in identity_prefix or "executable@sha256:" in identity_prefix:
        raise ContractError(
            "service.identity_prefix must not predeclare dynamic admission identity"
        )
    try:
        service_url = _normalize_loopback_url(
            _text(raw["service_url"], "service.service_url")
        )
    except ValueError as exc:
        raise ContractError(str(exc)) from exc

    launch = _object(raw["launch"], "service.launch")
    _strict_keys(
        launch,
        required={"cwd", "command"},
        optional={"env"},
        where="service.launch",
    )
    command_value = launch["command"]
    if not isinstance(command_value, list) or not command_value or not all(
        isinstance(item, str) and item for item in command_value
    ):
        raise ContractError("service.launch.command must be a non-empty string list")
    env = launch.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(key, str)
        and key
        and isinstance(item, str)
        and "=" not in key
        for key, item in env.items()
    ):
        raise ContractError("service.launch.env must map valid names to strings")

    resources = _object(raw["resources"], "service.resources")
    _strict_keys(
        resources,
        required={"gpu_count", "run_timeout_s"},
        optional={"estimate_s", "queue_timeout_s"},
        where="service.resources",
    )
    gpu_count = resources["gpu_count"]
    if isinstance(gpu_count, bool) or not isinstance(gpu_count, int) or gpu_count < 1:
        raise ContractError("service.resources.gpu_count must be a positive integer")
    estimate = resources.get("estimate_s")
    estimate_s = None if estimate is None else _positive(estimate, "estimate_s")
    queue_timeout = resources.get("queue_timeout_s")
    queue_timeout_s = (
        None
        if queue_timeout is None
        else _positive(queue_timeout, "queue_timeout_s")
    )
    return ManagedServiceSpec(
        service_id=service_id,
        description=str(raw.get("description", "")).strip(),
        owner=owner,
        service_url=service_url,
        source_root=_resolved_path(
            raw["source_root"], "service.source_root", source.parent
        ),
        identity_prefix=identity_prefix.rstrip("+"),
        cwd=_resolved_path(launch["cwd"], "service.launch.cwd", source.parent),
        command=tuple(command_value),
        env=dict(env),
        resources=ServiceResources(
            gpu_count=gpu_count,
            estimate_s=estimate_s,
            queue_timeout_s=queue_timeout_s,
            run_timeout_s=_positive(resources["run_timeout_s"], "run_timeout_s"),
        ),
        readiness_timeout_s=_positive(
            raw.get("readiness_timeout_s", 300.0), "readiness_timeout_s"
        ),
        idle_grace_s=(
            None
            if raw.get("idle_grace_s") is None
            else _positive(raw["idle_grace_s"], "idle_grace_s")
        ),
        raw=raw,
        digest=digest_json(raw),
        source_path=source,
    )
