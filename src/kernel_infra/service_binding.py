"""Bind one ready service deployment into one immutable task template."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import ContractError, digest_json, parse_task
from .store import utc_now

SERVICE_IDENTITY_TOKEN = "${KERNELINFRA_SERVICE_IDENTITY}"
DEPLOYMENT_RECEIPT_TOKEN = "${KERNELINFRA_DEPLOYMENT_RECEIPT}"
BINDING_SCHEMA = "kernelinfra.service-task-binding.v1"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def materialize_service_task(
    *,
    template_path: Path,
    output_path: Path,
    binding_path: Path,
    deployment_state: dict[str, Any],
    deployment_receipt: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    template = template_path.expanduser().resolve()
    output = output_path.expanduser().resolve()
    binding_output = binding_path.expanduser().resolve()
    if output.parent != template.parent or binding_output.parent != template.parent:
        raise ContractError(
            "task template, output, and binding receipt must share one directory"
        )
    try:
        raw = json.loads(template.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"task template not found: {template}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid task template JSON at {template}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContractError("task template must be an object")
    stages = raw.get("stages")
    if not isinstance(stages, list):
        raise ContractError("task template stages must be a list")
    service_stages = [
        stage
        for stage in stages
        if isinstance(stage, dict) and stage.get("execution") == "service"
    ]
    if len(service_stages) != 1:
        raise ContractError("task template must contain exactly one service stage")
    stage = service_stages[0]
    selected_stage_id = stage.get("id")
    judge = stage.get("judge")
    if not isinstance(judge, dict):
        raise ContractError("selected service stage judge must be an object")
    identity = judge.get("identity")
    if not isinstance(identity, str) or identity.count(SERVICE_IDENTITY_TOKEN) != 1:
        raise ContractError(
            "selected judge identity must contain exactly one service identity token"
        )
    command = judge.get("command")
    if not isinstance(command, list) or not all(
        isinstance(item, str) for item in command
    ):
        raise ContractError("selected service stage command must be a string list")
    option_indices = [
        index for index, item in enumerate(command) if item == "--deployment-receipt"
    ]
    if len(option_indices) != 1:
        raise ContractError(
            "selected service stage must have one --deployment-receipt option"
        )
    option_index = option_indices[0]
    if (
        option_index + 1 >= len(command)
        or command[option_index + 1] != DEPLOYMENT_RECEIPT_TOKEN
    ):
        raise ContractError(
            "--deployment-receipt must be followed by the deployment receipt token"
        )

    receipt_path = Path(str(deployment_state["deployment_receipt"])).resolve()
    service_identity = str(deployment_receipt["service_identity"])
    deployment_receipt_sha256 = digest_json(deployment_receipt)
    bound_identity = (
        f"{service_identity}"
        f"+deployment:{deployment_state['deployment_id']}"
        f"+deployment-receipt@sha256:{deployment_receipt_sha256}"
    )
    judge["identity"] = identity.replace(SERVICE_IDENTITY_TOKEN, bound_identity)
    command[option_index + 1] = str(receipt_path)
    encoded = json.dumps(raw, ensure_ascii=False)
    if SERVICE_IDENTITY_TOKEN in encoded or DEPLOYMENT_RECEIPT_TOKEN in encoded:
        raise ContractError("task template contains an unconsumed service token")

    task = parse_task(raw, source_path=output)
    binding = {
        "schema": BINDING_SCHEMA,
        "bound_at": utc_now(),
        "deployment_id": deployment_state["deployment_id"],
        "service_id": deployment_state["service_id"],
        "service_sha256": deployment_state["service_sha256"],
        "service_identity": service_identity,
        "deployment_receipt": str(receipt_path),
        "deployment_receipt_sha256": deployment_receipt_sha256,
        "template": str(template),
        "template_file_sha256": _file_sha256(template),
        "template_json_sha256": digest_json(json.loads(template.read_text())),
        "output": str(output),
        "binding_output": str(binding_output),
        "task_id": task.task_id,
        "task_sha256": task.digest,
        "stage_id": selected_stage_id,
    }
    return raw, {**binding, "binding_sha256": digest_json(binding)}
