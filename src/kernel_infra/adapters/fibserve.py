"""Translate a broker-managed PTXBench/FIBServe service result."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kernel_infra.service_attestation import (
    atomic_json,
    load_service_receipt,
    verify_service_receipt,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kernelinfra-fibserve")
    parser.add_argument("--deployment-receipt", type=Path, required=True)
    parser.add_argument("--definition", required=True)
    parser.add_argument("--entry-point", required=True)
    parser.add_argument(
        "--language",
        choices=("cuda", "cpp", "python", "triton", "tilelang"),
        default="cuda",
    )
    parser.add_argument("--binding", choices=("tvm-ffi", "torch"))
    parser.add_argument("--dependency", action="append", default=[])
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--workload", action="append", default=[])
    parser.add_argument("--author", default="kernel-infra")
    parser.add_argument("--name", default="kernelinfra-candidate")
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--poll-timeout", type=float, default=1800.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result_path = _required_env_path("KERNELINFRA_RESULT")
    stage_dir = _required_env_path("KERNELINFRA_STAGE_DIR")
    candidate_dir = _required_env_path("KERNELINFRA_CANDIDATE_DIR")
    try:
        deployment = load_service_receipt(args.deployment_receipt.resolve())
        _require_task_identity(deployment)
        verify_service_receipt(deployment)
        deployment_path = stage_dir / "deployment-receipt.json"
        atomic_json(deployment_path, deployment)
        service_url = str(deployment["service_url"])
        sources = _sources(candidate_dir, args.source)
        solution = {
            "name": args.name,
            "definition": args.definition,
            "author": args.author,
            "spec": {
                "language": args.language,
                "target_hardware": ["cuda"],
                "entry_point": args.entry_point,
                "dependencies": args.dependency,
                "destination_passing_style": True,
                **({"binding": args.binding} if args.binding else {}),
            },
            "sources": sources,
        }
        submit = _request_json(
            "POST",
            service_url + "/evaluate",
            {"solution": solution, "workload_uuids": args.workload or None},
            timeout=args.request_timeout,
        )
        task_id = submit["task_id"]
        deadline = time.monotonic() + args.poll_timeout
        response: dict[str, Any]
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"FIBServe task did not finish: {task_id}")
            query = urllib.parse.urlencode(
                {"timeout": min(30.0, max(0.0, deadline - time.monotonic()))}
            )
            response = _request_json(
                "GET",
                service_url + f"/tasks/{task_id}?{query}",
                None,
                timeout=max(args.request_timeout, 35.0),
            )
            if response.get("status") in {"completed", "failed"}:
                break
            time.sleep(args.poll_interval)
        atomic_json(stage_dir / "fibserve-response.json", response)
        verify_service_receipt(deployment)
        stage_result = _translate(response, sources, task_id, deployment)
        atomic_json(result_path, stage_result)
        return 0 if stage_result["status"] == "passed" else 1
    except Exception as exc:
        atomic_json(
            result_path,
            {
                "schema": "kernelinfra.stage-result.v1",
                "status": "failed",
                "validity": "unknown",
                "summary": f"FIBServe adapter error: {type(exc).__name__}: {exc}",
                "workloads": [],
            },
        )
        return 1


def _sources(candidate_dir: Path, selected: list[str]) -> list[dict[str, str]]:
    if selected:
        paths = [candidate_dir / value for value in selected]
    else:
        suffixes = {".cu", ".cuh", ".cc", ".cpp", ".h", ".hpp", ".py"}
        paths = sorted(
            path
            for path in candidate_dir.rglob("*")
            if path.is_file() and path.suffix in suffixes
        )
    if not paths:
        raise ValueError("candidate contains no selected FIBServe source files")
    result = []
    for path in paths:
        resolved = path.resolve()
        if candidate_dir.resolve() not in resolved.parents:
            raise ValueError(f"source escapes candidate snapshot: {path}")
        result.append(
            {
                "path": resolved.relative_to(candidate_dir.resolve()).as_posix(),
                "content": resolved.read_text(encoding="utf-8"),
            }
        )
    return result


def _translate(
    response: dict[str, Any],
    sources: list[dict[str, str]],
    task_id: str,
    deployment: dict[str, Any],
) -> dict[str, Any]:
    reported_commit = deployment["service_root"].get("commit")
    admission = deployment["broker_admission_receipt"]
    deployment_artifacts = {
        "fibserve_task_id": task_id,
        "deployment_receipt": "deployment-receipt.json",
        "broker_job_id": deployment["broker_job_id"],
        "broker_gpu_ids": deployment["broker_gpu_ids"],
    }
    deployment_fingerprints = {
        "deployment_receipt_sha256": _json_sha256(deployment),
        "service_identity": deployment["service_identity"],
        "broker_instance_id": deployment["broker_instance_id"],
        "broker_admission_receipt_sha256": admission["receipt_sha256"],
        "broker_launch_spec_sha256": admission["launch_spec_sha256"],
        "broker_argv_sha256": admission["argv_sha256"],
        "broker_env_sha256": admission["env_sha256"],
        "broker_effective_env_sha256": admission["effective_env_sha256"],
        "broker_executable_sha256": admission["executable_sha256"],
        "service_commit": reported_commit if isinstance(reported_commit, str) else "",
        "service_source_commit": deployment["source_commit"],
        "service_source_tree": deployment["source_tree"],
    }
    if response.get("status") == "failed":
        return {
            "schema": "kernelinfra.stage-result.v1",
            "status": "failed",
            "validity": "unknown",
            "summary": f"FIBServe task failed: {response.get('error', 'unknown')}",
            "workloads": [],
            "artifacts": deployment_artifacts,
            "fingerprints": deployment_fingerprints,
        }
    rows: list[dict[str, Any]] = []
    all_passed = True
    for trace in response.get("traces") or []:
        workload = trace.get("workload") or {}
        evaluation = trace.get("evaluation") or {}
        passed = evaluation.get("status") == "PASSED"
        all_passed = all_passed and passed
        row: dict[str, Any] = {
            "id": workload.get("uuid", ""),
            "correct": passed,
        }
        performance = evaluation.get("performance") or {}
        candidate_ms = performance.get("latency_ms")
        baseline_ms = performance.get("reference_latency_ms")
        if isinstance(candidate_ms, (int, float)) and candidate_ms > 0:
            row["candidate_ms"] = float(candidate_ms)
        if isinstance(baseline_ms, (int, float)) and baseline_ms > 0:
            row["baseline_ms"] = float(baseline_ms)
        rows.append(row)
    if not rows:
        return {
            "schema": "kernelinfra.stage-result.v1",
            "status": "failed",
            "validity": "unknown",
            "summary": "FIBServe completed without trace results",
            "workloads": [],
            "artifacts": deployment_artifacts,
            "fingerprints": deployment_fingerprints,
        }
    source_digest = hashlib.sha256()
    for source in sorted(sources, key=lambda item: item["path"]):
        source_digest.update(source["path"].encode())
        source_digest.update(source["content"].encode())
    return {
        "schema": "kernelinfra.stage-result.v1",
        "status": "passed" if all_passed else "failed",
        "validity": "valid" if all_passed else "invalid",
        "summary": (
            "all FIBServe workloads passed"
            if all_passed
            else "one or more FIBServe workloads failed"
        ),
        "workloads": rows,
        "artifacts": {
            **deployment_artifacts,
            "raw_response": "fibserve-response.json",
        },
        "fingerprints": {
            "source_sha256": source_digest.hexdigest(),
            **deployment_fingerprints,
        },
    }


def _require_task_identity(deployment: dict[str, Any]) -> None:
    task_path = _required_env_path("KERNELINFRA_TASK")
    stage_id = os.environ.get("KERNELINFRA_STAGE_ID")
    task = json.loads(task_path.read_text(encoding="utf-8"))
    identities = [
        stage.get("judge", {}).get("identity")
        for stage in task.get("stages", [])
        if stage.get("id") == stage_id
    ]
    if len(identities) != 1 or str(deployment["service_identity"]) not in str(
        identities[0]
    ):
        raise RuntimeError("task judge identity does not bind service deployment identity")


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _request_json(
    method: str, url: str, value: dict[str, Any] | None, *, timeout: float
) -> dict[str, Any]:
    data = None if value is None else json.dumps(value).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"FIBServe HTTP {exc.code}: {detail}") from exc
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("FIBServe returned a non-object response")
    return result


def _required_env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return Path(value).resolve()


if __name__ == "__main__":
    raise SystemExit(main())
