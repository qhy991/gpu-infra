"""Real-GPU infrastructure smoke with balanced AB/BA timing."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import statistics
import tempfile
import time
from pathlib import Path

import torch

WORKLOAD = "vector-1048576"


def _load_candidate(candidate_dir: Path):
    path = candidate_dir / "kernel.py"
    spec = importlib.util.spec_from_file_location("kernelinfra_candidate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load candidate: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "run", None)):
        raise RuntimeError("candidate must define callable run(x)")
    return module, path


def _elapsed_ms(function, x) -> float:
    start = torch.cuda.Event(enable_timing=True)
    end = torch.cuda.Event(enable_timing=True)
    start.record()
    function(x)
    end.record()
    end.synchronize()
    return float(start.elapsed_time(end))


def _benchmark(candidate, x):
    baseline = lambda value: value * 2.0
    for _ in range(10):
        baseline(x)
        candidate.run(x)
    torch.cuda.synchronize()
    baseline_samples = []
    candidate_samples = []
    for index in range(20):
        if index % 2 == 0:
            baseline_samples.append(_elapsed_ms(baseline, x))
            candidate_samples.append(_elapsed_ms(candidate.run, x))
        else:
            candidate_samples.append(_elapsed_ms(candidate.run, x))
            baseline_samples.append(_elapsed_ms(baseline, x))
    baseline_ms = statistics.median(baseline_samples)
    candidate_ms = statistics.median(candidate_samples)
    baseline_cv = statistics.pstdev(baseline_samples) / statistics.mean(
        baseline_samples
    )
    return baseline_ms, candidate_ms, baseline_samples, candidate_samples, baseline_cv


def _write(path: Path, value) -> None:
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


def main() -> int:
    result_path = Path(os.environ["KERNELINFRA_RESULT"])
    candidate_dir = Path(os.environ["KERNELINFRA_CANDIDATE_DIR"])
    stage_kind = os.environ["KERNELINFRA_STAGE_KIND"]
    try:
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available inside the broker allocation")
        candidate, source_path = _load_candidate(candidate_dir)
        torch.manual_seed(20260824)
        x = torch.randn(1 << 20, device="cuda", dtype=torch.float32)
        actual = candidate.run(x)
        expected = x * 2.0
        correct = bool(torch.equal(actual, expected))
        device = torch.cuda.get_device_name(0)
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if not correct:
            result = {
                "schema": "kernelinfra.stage-result.v1",
                "status": "failed",
                "validity": "invalid",
                "summary": f"candidate output differs from reference on {device}",
                "workloads": [{"id": WORKLOAD, "correct": False}],
                "fingerprints": {"source_sha256": source_sha256},
            }
        elif stage_kind == "correctness":
            # A visible overlap window proves shared broker capacity in the pilot.
            time.sleep(4)
            result = {
                "schema": "kernelinfra.stage-result.v1",
                "status": "passed",
                "validity": "valid",
                "summary": f"exact correctness passed on {device}",
                "workloads": [{"id": WORKLOAD, "correct": True}],
                "fingerprints": {"source_sha256": source_sha256},
            }
        elif stage_kind == "benchmark":
            baseline_ms, candidate_ms, baseline_samples, candidate_samples, cv = (
                _benchmark(candidate, x)
            )
            result = {
                "schema": "kernelinfra.stage-result.v1",
                "status": "passed",
                "validity": "valid",
                "summary": (
                    f"balanced AB/BA completed on {device}; baseline_cv={cv:.6f}"
                ),
                "workloads": [
                    {
                        "id": WORKLOAD,
                        "correct": True,
                        "candidate_ms": candidate_ms,
                        "baseline_ms": baseline_ms,
                        "candidate_samples_ms": candidate_samples,
                        "baseline_samples_ms": baseline_samples,
                        "stable": cv <= 0.1,
                    }
                ],
                "fingerprints": {"source_sha256": source_sha256},
            }
        else:
            raise RuntimeError(f"unsupported smoke stage kind: {stage_kind}")
        _write(result_path, result)
        return 0 if result["status"] == "passed" else 1
    except Exception as exc:
        _write(
            result_path,
            {
                "schema": "kernelinfra.stage-result.v1",
                "status": "failed",
                "validity": "unknown",
                "summary": f"smoke evaluator error: {type(exc).__name__}: {exc}",
                "workloads": [],
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
