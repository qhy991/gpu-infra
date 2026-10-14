import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from kernel_infra.candidate import CandidateError, snapshot_candidate
from kernel_infra.contracts import ContractError, load_task


def task_value(tmp: Path) -> dict:
    return {
        "schema": "kernelinfra.task.v1",
        "task_id": "unit-task",
        "workloads": ["w0"],
        "comparison": {
            "primary_workloads": ["w0"],
            "relative_noise_floor": 0.02,
        },
        "stages": [
            {
                "id": "correctness",
                "kind": "correctness",
                "judge": {
                    "identity": "judge@test",
                    "cwd": str(tmp),
                    "command": ["python3", "judge.py"],
                },
                "resources": {
                    "mode": "shared",
                    "gpu_count": 1,
                    "run_timeout_s": 30,
                },
            },
            {
                "id": "bench",
                "kind": "benchmark",
                "judge": {
                    "identity": "judge@test",
                    "cwd": str(tmp),
                    "command": ["python3", "judge.py"],
                },
                "resources": {
                    "mode": "exclusive",
                    "gpu_count": 1,
                    "run_timeout_s": 30,
                },
            },
        ],
    }


class ContractTests(unittest.TestCase):
    def test_optional_gpu_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "task.json"
            for scope, count, expected in (
                (None, 1, None), ([1], 1, (1,)), ([3, 1], 2, (3, 1)),
            ):
                with self.subTest(scope=scope):
                    value = task_value(root)
                    value["stages"][0]["resources"].update(
                        allowed_gpu_ids=scope, gpu_count=count
                    )
                    path.write_text(json.dumps(value))
                    task = load_task(path)
                    self.assertEqual(task.stages[0].resources.allowed_gpu_ids, expected)
                    self.assertIsNone(task.stages[1].resources.allowed_gpu_ids)

    def test_invalid_gpu_scopes_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "task.json"
            for scope, count in (
                ([], 1), ([1, 1], 1), ([-1], 1), ([True], 1), ([False], 1),
                ([1.0], 1), (["1"], 1), (1, 1), ("1", 1), ({"gpu": 1}, 1),
                ([1], 2),
            ):
                with self.subTest(scope=scope, count=count):
                    value = task_value(root)
                    value["stages"][0]["resources"].update(
                        allowed_gpu_ids=scope, gpu_count=count
                    )
                    path.write_text(json.dumps(value))
                    with self.assertRaisesRegex(ContractError, "allowed_gpu_ids"):
                        load_task(path)

    def test_non_broker_stage_refuses_gpu_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "task.json"
            for execution in ("local", "service"):
                with self.subTest(execution=execution):
                    value = task_value(root)
                    value["stages"] = value["stages"][:1]
                    value["stages"][0]["execution"] = execution
                    value["stages"][0]["resources"]["allowed_gpu_ids"] = [1]
                    path.write_text(json.dumps(value))
                    with self.assertRaisesRegex(ContractError, "resources must be omitted"):
                        load_task(path)

    def test_checked_in_smoke_judge_identity_matches_source(self):
        root = Path(__file__).resolve().parents[1]
        evaluator = root / "examples" / "a800_smoke" / "evaluator.py"
        digest = hashlib.sha256(evaluator.read_bytes()).hexdigest()
        task = load_task(root / "examples" / "a800_smoke" / "task.json")
        self.assertEqual(
            {stage.judge_identity for stage in task.stages},
            {f"a800-smoke-evaluator@sha256:{digest}"},
        )

    def test_valid_staged_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "task.json"
            path.write_text(json.dumps(task_value(root)))
            task = load_task(path)
            self.assertEqual(task.task_id, "unit-task")
            self.assertEqual([stage.resources.mode for stage in task.stages], ["shared", "exclusive"])
            self.assertEqual(len(task.digest), 64)

    def test_benchmark_must_be_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = task_value(root)
            value["stages"][1]["resources"]["mode"] = "shared"
            path = root / "task.json"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "must use exclusive"):
                load_task(path)

    def test_service_stage_owns_no_per_request_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = task_value(root)
            value["stages"] = [
                {
                    "id": "fibserve",
                    "kind": "judge",
                    "execution": "service",
                    "judge": {
                        "identity": "fibserve@commit",
                        "cwd": str(root),
                        "command": ["kernelinfra-fibserve", "--help"],
                    },
                }
            ]
            path = root / "task.json"
            path.write_text(json.dumps(value))
            task = load_task(path)
            self.assertEqual(task.stages[0].execution, "service")
            self.assertIsNone(task.stages[0].resources)

    def test_candidate_snapshot_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "real.py").write_text("value = 1\n")
            (source / "link.py").symlink_to(source / "real.py")
            with self.assertRaises(CandidateError):
                snapshot_candidate(source, root / "snapshot")


if __name__ == "__main__":
    unittest.main()
