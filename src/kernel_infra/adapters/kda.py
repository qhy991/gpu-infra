"""Import one authoritative KDA ledger reference without inventing timing."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kernel_infra.service_attestation import atomic_json

LEDGER_REQUIRED = {
    "submission_id",
    "kernel",
    "source",
    "correctness_passed",
    "all_geomean",
    "large_geomean",
    "small_geomean",
    "benchmarks_sha",
    "artifact_digest",
    "gpu_model",
    "cuda_version",
    "scoring_schema_version",
    "per_workload_file",
}
PER_WORKLOAD_REQUIRED = {
    "submission_id",
    "kernel",
    "workload_id",
    "size_class",
    "passed",
    "status",
    "speedup",
    "note",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kernelinfra-kda-import")
    parser.add_argument("--report-ref", type=Path, required=True)
    parser.add_argument("--expected-kernel", required=True)
    parser.add_argument("--expected-benchmarks-sha", required=True)
    parser.add_argument("--expected-scoring-schema", default="1")
    parser.add_argument("--expected-workloads", type=int, required=True)
    parser.add_argument("--judge-identity", required=True)
    return parser


def _bool(value: str, where: str) -> bool:
    if value == "True":
        return True
    if value == "False":
        return False
    raise RuntimeError(f"{where} must be True or False")


def _optional_float(value: str, where: str) -> float | None:
    if value == "":
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise RuntimeError(f"{where} is not numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise RuntimeError(f"{where} must be finite and positive")
    return number


def _geomean(values: list[float]) -> float | None:
    if not values:
        return None
    return math.exp(sum(math.log(value) for value in values) / len(values))


def _same_metric(stored: float | None, expected: float | None) -> bool:
    if (stored is None) != (expected is None):
        return False
    return stored is None or abs(stored - expected) <= 1e-6 * max(1.0, abs(expected))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _task_identity(expected: str) -> None:
    task_path = Path(os.environ["KERNELINFRA_TASK"])
    stage_id = os.environ["KERNELINFRA_STAGE_ID"]
    task = json.loads(task_path.read_text(encoding="utf-8"))
    identities = [
        stage.get("judge", {}).get("identity")
        for stage in task.get("stages", [])
        if stage.get("id") == stage_id
    ]
    if len(identities) != 1 or expected not in str(identities[0]):
        raise RuntimeError("task judge identity does not bind KDA judge identity")


def import_report(args: argparse.Namespace, stage_dir: Path) -> dict[str, Any]:
    report_path = args.report_ref.resolve()
    reference = json.loads(report_path.read_text(encoding="utf-8"))
    if set(reference) != {"ledger_row", "ledger_path"}:
        raise RuntimeError("KDA report reference has unexpected fields")
    row_index = reference["ledger_row"]
    if not isinstance(row_index, int) or row_index < 1:
        raise RuntimeError("KDA ledger row must be a positive integer")
    ledger_path = Path(reference["ledger_path"]).resolve()
    with ledger_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not LEDGER_REQUIRED.issubset(reader.fieldnames):
            raise RuntimeError("KDA ledger is missing required columns")
        rows = list(reader)
    if row_index > len(rows):
        raise RuntimeError("KDA ledger row is out of range")
    ledger = rows[row_index - 1]
    if ledger["source"] != "authoritative":
        raise RuntimeError("KDA ledger source is not authoritative")
    if ledger["kernel"] != args.expected_kernel:
        raise RuntimeError("KDA kernel does not match the task")
    if ledger["benchmarks_sha"] != args.expected_benchmarks_sha:
        raise RuntimeError("KDA benchmark revision drift")
    if ledger["scoring_schema_version"] != args.expected_scoring_schema:
        raise RuntimeError("KDA scoring schema drift")

    per_workload_path = Path(ledger["per_workload_file"]).resolve()
    with per_workload_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not PER_WORKLOAD_REQUIRED.issubset(
            reader.fieldnames
        ):
            raise RuntimeError("KDA per-workload receipt is missing required columns")
        raw_workloads = list(reader)
    if len(raw_workloads) != args.expected_workloads:
        raise RuntimeError(
            f"KDA workload count drift: expected {args.expected_workloads}, "
            f"got {len(raw_workloads)}"
        )

    workloads = []
    scored: dict[str, list[float]] = {"all": [], "large": [], "small": []}
    for index, raw in enumerate(raw_workloads):
        where = f"workload[{index}]"
        if raw["submission_id"] != ledger["submission_id"]:
            raise RuntimeError(f"{where} submission id drift")
        if raw["kernel"] != ledger["kernel"]:
            raise RuntimeError(f"{where} kernel drift")
        if raw["size_class"] not in {"small", "large"}:
            raise RuntimeError(f"{where} size class is invalid")
        passed = _bool(raw["passed"], f"{where}.passed")
        speedup = _optional_float(raw["speedup"], f"{where}.speedup")
        status = raw["status"]
        if status == "ok" and not (passed and speedup is not None):
            raise RuntimeError(f"{where} status disagrees with passed/speedup")
        if status == "candidate_only" and not (passed and speedup is None):
            raise RuntimeError(f"{where} candidate_only is inconsistent")
        if status in {"incorrect", "error"} and passed:
            raise RuntimeError(f"{where} failed status cannot pass")
        if status not in {"ok", "candidate_only", "incorrect", "error"}:
            raise RuntimeError(f"{where} status is invalid")
        if passed and speedup is not None:
            scored["all"].append(speedup)
            scored[raw["size_class"]].append(speedup)
        item: dict[str, Any] = {
            "id": raw["workload_id"],
            "correct": passed,
            "notes": (
                f"size_class={raw['size_class']} status={status} "
                f"note={raw['note']}"
            ).strip(),
        }
        if speedup is not None:
            item["speedup"] = speedup
        workloads.append(item)

    geomeans = {
        key: _optional_float(ledger[f"{key}_geomean"], f"{key}_geomean")
        for key in ("all", "large", "small")
    }
    for key in ("all", "large", "small"):
        if not _same_metric(geomeans[key], _geomean(scored[key])):
            raise RuntimeError(f"KDA {key} geomean disagrees with workload rows")
    correctness = _bool(ledger["correctness_passed"], "correctness_passed")
    if correctness != all(item["correct"] for item in workloads):
        raise RuntimeError("KDA correctness summary disagrees with workload rows")

    atomic_json(stage_dir / "kda-report-ref.json", reference)
    atomic_json(stage_dir / "kda-ledger-row.json", ledger)
    (stage_dir / "kda-per-workload.csv").write_bytes(per_workload_path.read_bytes())
    status = "passed" if correctness else "failed"
    return {
        "schema": "kernelinfra.stage-result.v1",
        "status": status,
        "validity": "valid" if correctness else "invalid",
        "summary": (
            f"authoritative KDA correctness {sum(item['correct'] for item in workloads)}/"
            f"{len(workloads)}; speedup-only receipt, absolute timing and full "
            "provenance unavailable"
        ),
        "workloads": workloads,
        "metrics": {
            "primary_metric": "large_geomean",
            "geomeans": geomeans,
            "absolute_timing_available": False,
            "provenance_complete": False,
            "missing_provenance_fields": ["judge_image_digest", "driver_version"],
        },
        "artifacts": {
            "report_ref": "kda-report-ref.json",
            "ledger_row": "kda-ledger-row.json",
            "per_workload": "kda-per-workload.csv",
            "authoritative_row_index": row_index,
        },
        "fingerprints": {
            "submission_id": ledger["submission_id"],
            "benchmarks_sha": ledger["benchmarks_sha"],
            "artifact_digest": ledger["artifact_digest"],
            "gpu_model": ledger["gpu_model"],
            "cuda_version": ledger["cuda_version"],
            "scoring_schema_version": ledger["scoring_schema_version"],
            "ledger_sha256": _sha256(ledger_path),
            "per_workload_sha256": _sha256(per_workload_path),
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result_path = Path(os.environ["KERNELINFRA_RESULT"])
    stage_dir = Path(os.environ["KERNELINFRA_STAGE_DIR"])
    try:
        _task_identity(args.judge_identity)
        result = import_report(args, stage_dir)
    except Exception as exc:
        result = {
            "schema": "kernelinfra.stage-result.v1",
            "status": "failed",
            "validity": "unknown",
            "summary": f"KDA importer error: {type(exc).__name__}: {exc}",
            "workloads": [],
        }
    atomic_json(result_path, result)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
