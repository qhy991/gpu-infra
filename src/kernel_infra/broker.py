"""Bounded requests to the machine-local broker; no allocation policy."""

from __future__ import annotations

import json
import socket
import struct
from pathlib import Path
from typing import Any


def _broker_request(
    socket_path: Path, request: dict[str, Any], *, timeout_s: float = 10.0
) -> tuple[dict[str, Any], dict[str, int | None]]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_s)
            client.connect(str(socket_path.expanduser().resolve()))
            peer = {"pid": None, "uid": None, "gid": None}
            if hasattr(socket, "SO_PEERCRED"):
                raw_peer = client.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                )
                peer["pid"], peer["uid"], peer["gid"] = struct.unpack("3i", raw_peer)
            connection = client.makefile("rwb")
            with connection:
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


def cancel_broker_job(socket_path: Path, job_id: str) -> bool:
    response, _peer = _broker_request(
        socket_path, {"op": "cancel", "job_id": job_id}, timeout_s=30.0
    )
    if response.get("type") != "cancelled" or not isinstance(response.get("ok"), bool):
        raise RuntimeError(f"invalid broker cancel response: {response!r}")
    return response["ok"]
