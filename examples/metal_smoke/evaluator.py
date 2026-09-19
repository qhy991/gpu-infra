"""Correctness-only Metal judge. No performance/frontier claim."""
import json
import os
from pathlib import Path
import subprocess


def main():
    result = {"schema": "kernelinfra.stage-result.v1", "status": "failed", "validity": "unknown"}
    try:
        stage = os.environ["KERNELINFRA_STAGE_KIND"]
        binary = Path(os.environ["KERNELINFRA_RUN_DIR"]) / "metal-judge"
        if stage == "compile":
            subprocess.run(["xcrun", "swiftc", "judge.swift", "-framework", "Metal", "-framework", "CoreGraphics", "-o", str(binary)], check=True)
            result.update(status="passed", validity="unknown", summary="CPU-only Swift host compilation")
        else:
            if os.environ.get("GPUQ_BACKEND") != "metal":
                raise RuntimeError("Metal broker allocation required")
            command = subprocess.run([str(binary), str(Path(os.environ["KERNELINFRA_CANDIDATE_DIR"]) / "kernel.metal")], capture_output=True, text=True, check=True)
            if command.stdout.strip() not in {"correct", "incorrect"}:
                raise RuntimeError("malformed Metal judge output")
            correct = command.stdout.strip() == "correct"
            result.update(status="passed" if correct else "failed", validity="valid" if correct else "invalid", workloads=[{"id": "vector-65536", "correct": correct}], metrics={"backend": "metal", "occupancy_scope": os.environ["GPUQ_OCCUPANCY_SCOPE"]})
    except Exception as exc:
        result["summary"] = str(exc)
    Path(os.environ["KERNELINFRA_RESULT"]).write_text(json.dumps(result))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
