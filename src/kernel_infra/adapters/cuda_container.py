"""Container-backed NVCC, correctness, and benchmark judge for A800."""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

STAGE_SCHEMA = "kernelinfra.stage-result.v1"
_ACTIVE_CONTAINER: str | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--arch", default="sm_80")
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--judge-dir", type=Path, default=Path.cwd())
    return parser


def bundle_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    sources = (
        ("cuda_container.py", Path(__file__).resolve()),
        ("harness.cu", root / "harness.cu"),
    )
    for name, path in sources:
        data = path.read_bytes()
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return Path(value).resolve()


def _container_name() -> str:
    run_id = os.environ["KERNELINFRA_RUN_ID"]
    stage_id = os.environ["KERNELINFRA_STAGE_ID"]
    return f"kernelinfra-{run_id[-12:]}-{stage_id}"


def _cleanup_container() -> None:
    global _ACTIVE_CONTAINER
    name = _ACTIVE_CONTAINER
    if not name:
        return
    deadline = time.monotonic() + 3.0
    while True:
        subprocess.run(
            ["docker", "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        present = subprocess.run(
            ["docker", "ps", "--all", "--quiet", "--filter", f"name=^/{name}$"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if not present.stdout.strip() or time.monotonic() >= deadline:
            _ACTIVE_CONTAINER = None
            return
        time.sleep(0.1)


def _install_cleanup(container_name: str) -> None:
    global _ACTIVE_CONTAINER
    _ACTIVE_CONTAINER = container_name
    atexit.register(_cleanup_container)

    def stop(signum: int, _frame: object) -> None:
        _cleanup_container()
        raise SystemExit(128 + signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)


def _docker_base(
    *,
    image_id: str,
    candidate_dir: Path,
    judge_dir: Path,
    artifact_dir: Path,
    needs_gpu: bool,
) -> list[str]:
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--name",
        _container_name(),
        "--label",
        f"kernelinfra.run_id={os.environ['KERNELINFRA_RUN_ID']}",
        "--label",
        f"kernelinfra.stage_id={os.environ['KERNELINFRA_STAGE_ID']}",
    ]
    if needs_gpu:
        physical_gpus = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
        if not physical_gpus:
            raise RuntimeError("broker did not assign CUDA_VISIBLE_DEVICES")
        command.extend(["--gpus", f"device={physical_gpus}"])
    command.extend(
        [
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--volume",
            f"{candidate_dir}:/candidate:ro",
            "--volume",
            f"{judge_dir}:/judge:ro",
            "--volume",
            f"{artifact_dir}:/artifacts:rw",
            image_id,
        ]
    )
    return command


def _run(command: list[str], *, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _last_json_object(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        value = json.loads(stripped)
        if not isinstance(value, dict):
            raise ValueError("harness JSON must be an object")
        return value
    raise ValueError("harness emitted no JSON object")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _image_identity(image: str) -> str:
    completed = _run(["docker", "image", "inspect", image, "--format", "{{.Id}}"], timeout=30)
    if completed.returncode != 0:
        raise RuntimeError(f"cannot inspect image {image}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _fingerprints(
    *, candidate: Path, binary: Path, sass: Path, ptx: Path, image_id: str
) -> dict[str, str]:
    return {
        "source_sha256": _sha256(candidate),
        "binary_sha256": _sha256(binary),
        "sass_sha256": _sha256(sass),
        "ptx_sha256": _sha256(ptx),
        "container_image_id": image_id,
    }


def _compile(
    *, base: list[str], arch: str, artifact_dir: Path, stage_dir: Path
) -> tuple[bool, str]:
    command = [
        *base,
        "nvcc",
        "-O3",
        "-std=c++17",
        f"-arch={arch}",
        "-lineinfo",
        "-Xptxas",
        "-v",
        "/judge/harness.cu",
        "/candidate/kernel.cu",
        "-o",
        "/artifacts/runner",
    ]
    completed = _run(command)
    _atomic_text(stage_dir / "compile.stdout.log", completed.stdout)
    _atomic_text(stage_dir / "compile.stderr.log", completed.stderr)
    if completed.returncode != 0:
        return False, f"nvcc failed with exit {completed.returncode}"

    for option, name in (("-sass", "runner.sass"), ("-ptx", "runner.ptx")):
        inspected = _run([*base, "cuobjdump", option, "/artifacts/runner"])
        _atomic_text(artifact_dir / name, inspected.stdout)
        _atomic_text(stage_dir / f"cuobjdump{option}.stderr.log", inspected.stderr)
        if inspected.returncode != 0:
            return False, f"cuobjdump {option} failed with exit {inspected.returncode}"
    return True, ""


def _judge_identity(task_path: Path, stage_id: str) -> str:
    task = json.loads(task_path.read_text(encoding="utf-8"))
    for stage in task["stages"]:
        if stage["id"] == stage_id:
            return str(stage["judge"]["identity"])
    raise RuntimeError(f"stage is absent from task: {stage_id}")


def _write_failure(
    result_path: Path,
    *,
    validity: str,
    summary: str,
    artifacts: dict[str, Any] | None = None,
) -> int:
    _atomic_json(
        result_path,
        {
            "schema": STAGE_SCHEMA,
            "status": "failed",
            "validity": validity,
            "summary": summary,
            "workloads": [],
            "artifacts": artifacts or {},
        },
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result_path = _required_path("KERNELINFRA_RESULT")
    stage_dir = _required_path("KERNELINFRA_STAGE_DIR")
    run_dir = _required_path("KERNELINFRA_RUN_DIR")
    task_path = _required_path("KERNELINFRA_TASK")
    candidate_dir = _required_path("KERNELINFRA_CANDIDATE_DIR")
    stage_id = os.environ["KERNELINFRA_STAGE_ID"]
    stage_kind = os.environ["KERNELINFRA_STAGE_KIND"]
    _install_cleanup(_container_name())
    judge_dir = args.judge_dir.resolve()
    artifact_name = args.artifact_name
    candidate = candidate_dir / "kernel.cu"
    artifact_dir = run_dir / "artifacts" / artifact_name
    artifact_dir.mkdir(parents=True, exist_ok=True)
    binary = artifact_dir / "runner"
    sass = artifact_dir / "runner.sass"
    ptx = artifact_dir / "runner.ptx"
    compiler_artifacts = {
        "binary": f"artifacts/{artifact_name}/runner",
        "sass": f"artifacts/{artifact_name}/runner.sass",
        "ptx": f"artifacts/{artifact_name}/runner.ptx",
    }

    try:
        actual_image_id = _image_identity(args.image)
        if actual_image_id != args.image_id:
            raise RuntimeError(
                f"container image drift: expected {args.image_id}, got {actual_image_id}"
            )
        bundle = bundle_sha256(judge_dir)
        identity = _judge_identity(task_path, stage_id)
        if bundle not in identity or args.image_id not in identity:
            raise RuntimeError("task judge identity does not bind evaluator bundle and image")
        if not candidate.is_file():
            return _write_failure(
                result_path,
                validity="invalid",
                summary="candidate/kernel.cu is missing",
            )

        base = _docker_base(
            image_id=args.image_id,
            candidate_dir=candidate_dir,
            judge_dir=judge_dir,
            artifact_dir=artifact_dir,
            needs_gpu=stage_kind != "compile",
        )
        if stage_kind == "compile":
            compiled, compile_error = _compile(
                base=base, arch=args.arch, artifact_dir=artifact_dir, stage_dir=stage_dir
            )
            if not compiled:
                return _write_failure(
                    result_path,
                    validity="invalid",
                    summary=compile_error,
                    artifacts={
                        "compile_stdout": "compile.stdout.log",
                        "compile_stderr": "compile.stderr.log",
                    },
                )
            result = {
                "schema": STAGE_SCHEMA,
                "status": "passed",
                "validity": "unknown",
                "summary": f"NVCC {args.arch} compile and compiler evidence passed",
                "workloads": [],
                "artifacts": {
                    **compiler_artifacts,
                    "compile_stdout": "compile.stdout.log",
                    "compile_stderr": "compile.stderr.log",
                },
                "fingerprints": _fingerprints(
                    candidate=candidate,
                    binary=binary,
                    sass=sass,
                    ptx=ptx,
                    image_id=args.image_id,
                ),
            }
            _atomic_json(result_path, result)
            return 0
        elif stage_kind in {"correctness", "sanitize", "benchmark"}:
            if not all(path.is_file() for path in (binary, sass, ptx)):
                return _write_failure(
                    result_path,
                    validity="unknown",
                    summary="compile-stage binary or compiler evidence is missing",
                )
        else:
            return _write_failure(
                result_path,
                validity="unknown",
                summary=f"unsupported stage kind: {stage_kind}",
            )

        if stage_kind == "sanitize":
            sanitizer_results: list[subprocess.CompletedProcess[str]] = []
            for tool in ("memcheck", "racecheck"):
                sanitized = _run(
                    [
                        *base,
                        "compute-sanitizer",
                        "--tool",
                        tool,
                        "--error-exitcode",
                        "99",
                        "/artifacts/runner",
                        "--mode",
                        "correctness",
                    ]
                )
                sanitizer_results.append(sanitized)
                _atomic_text(stage_dir / f"{tool}.stdout.log", sanitized.stdout)
                _atomic_text(stage_dir / f"{tool}.stderr.log", sanitized.stderr)
                if sanitized.returncode != 0:
                    break
            completed = sanitizer_results[-1]
        else:
            execution = [*base, "/artifacts/runner", "--mode", stage_kind]
            completed = _run(execution)
        _atomic_text(stage_dir / "harness.stdout.log", completed.stdout)
        _atomic_text(stage_dir / "harness.stderr.log", completed.stderr)
        try:
            payload = _last_json_object(completed.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            if stage_kind == "correctness" and completed.returncode != 0:
                summary = (
                    "candidate runtime failed before a complete correctness result "
                    f"(exit {completed.returncode})"
                )
            elif stage_kind == "sanitize" and completed.returncode != 0:
                summary = (
                    "compute-sanitizer rejected candidate before a complete result "
                    f"(exit {completed.returncode})"
                )
            else:
                summary = f"harness failed without valid JSON: {exc}"
            return _write_failure(
                result_path,
                validity=(
                    "invalid"
                    if stage_kind in {"correctness", "sanitize"}
                    and completed.returncode != 0
                    else "unknown"
                ),
                summary=summary,
                artifacts={
                    "harness_stdout": "harness.stdout.log",
                    "harness_stderr": "harness.stderr.log",
                },
            )
        _atomic_json(stage_dir / "harness-output.json", payload)
        fingerprints = _fingerprints(
            candidate=candidate,
            binary=binary,
            sass=sass,
            ptx=ptx,
            image_id=args.image_id,
        )
        artifacts = {
            **compiler_artifacts,
            "harness_output": "harness-output.json",
        }

        if stage_kind in {"correctness", "sanitize"}:
            workloads = []
            for row in payload["workloads"]:
                notes = f"max_abs_error={float(row['max_abs_error']):.9g}"
                if "max_rel_error" in row:
                    notes += f" max_rel_error={float(row['max_rel_error']):.9g}"
                workloads.append(
                    {
                        "id": row["id"],
                        "correct": bool(row["correct"]),
                        "notes": notes,
                    }
                )
            passed = completed.returncode == 0 and all(
                row["correct"] for row in workloads
            )
            if stage_kind == "sanitize":
                passed_summary = (
                    f"compute-sanitizer memcheck+racecheck passed on {payload['device']}"
                )
                failed_summary = (
                    f"compute-sanitizer rejected candidate on {payload['device']}"
                )
                for tool in ("memcheck", "racecheck"):
                    if (stage_dir / f"{tool}.stderr.log").is_file():
                        artifacts[f"{tool}_stderr"] = f"{tool}.stderr.log"
            else:
                passed_summary = f"CUDA correctness passed on {payload['device']}"
                failed_summary = (
                    f"candidate failed correctness on {payload['device']}"
                )
            result = {
                "schema": STAGE_SCHEMA,
                "status": "passed" if passed else "failed",
                "validity": "valid" if passed else "invalid",
                "summary": (
                    passed_summary if passed else failed_summary
                ),
                "workloads": workloads,
                "artifacts": artifacts,
                "fingerprints": fingerprints,
            }
        else:
            task = json.loads(task_path.read_text(encoding="utf-8"))
            noise = float(task["comparison"]["relative_noise_floor"])
            workloads = [
                {
                    "id": row["id"],
                    "correct": True,
                    "candidate_ms": float(row["candidate_ms"]),
                    "baseline_ms": float(row["baseline_ms"]),
                    "candidate_samples_ms": [
                        float(value) for value in row["candidate_samples_ms"]
                    ],
                    "baseline_samples_ms": [
                        float(value) for value in row["baseline_samples_ms"]
                    ],
                    "stable": float(row["baseline_cv"]) <= noise,
                    "notes": f"baseline_cv={float(row['baseline_cv']):.9g}",
                }
                for row in payload["workloads"]
            ]
            result = {
                "schema": STAGE_SCHEMA,
                "status": "passed" if completed.returncode == 0 else "failed",
                "validity": "valid" if completed.returncode == 0 else "unknown",
                "summary": f"balanced AB/BA completed on {payload['device']}",
                "workloads": workloads,
                "artifacts": artifacts,
                "fingerprints": fingerprints,
            }
        _atomic_json(result_path, result)
        return 0 if result["status"] == "passed" else 1
    except (OSError, RuntimeError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        return _write_failure(
            result_path,
            validity="unknown",
            summary=f"container evaluator error: {type(exc).__name__}: {exc}",
        )


if __name__ == "__main__":
    raise SystemExit(main())
