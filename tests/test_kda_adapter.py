import argparse
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kernel_infra.adapters import kda
from kernel_infra.contracts import load_task
from kernel_infra.results import aggregate_run_result, validate_stage_result


class KdaAdapterTests(unittest.TestCase):
    def test_source_checkout_cli_bootstraps_without_pythonpath(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("kda.py", "fibserve.py"):
            completed = subprocess.run(
                [sys.executable, str(root / "src" / "kernel_infra" / "adapters" / name), "--help"],
                cwd=root,
                env={},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def fixture(self, root: Path, *, large_geomean: str = "2.0"):
        per_workload = root / "per.csv"
        with per_workload.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=sorted(kda.PER_WORKLOAD_REQUIRED))
            writer.writeheader()
            writer.writerow(
                {
                    "submission_id": "sub-1",
                    "kernel": "kda_forward",
                    "workload_id": "large-0",
                    "size_class": "large",
                    "passed": "True",
                    "status": "ok",
                    "speedup": "2.0",
                    "note": "",
                }
            )
            writer.writerow(
                {
                    "submission_id": "sub-1",
                    "kernel": "kda_forward",
                    "workload_id": "small-0",
                    "size_class": "small",
                    "passed": "True",
                    "status": "ok",
                    "speedup": "1.0",
                    "note": "",
                }
            )
        ledger = root / "ledger.csv"
        fieldnames = sorted(kda.LEDGER_REQUIRED | {"per_workload_file"})
        with ledger.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(
                {
                    "submission_id": "sub-1",
                    "kernel": "kda_forward",
                    "source": "authoritative",
                    "correctness_passed": "True",
                    "all_geomean": str(2.0**0.5),
                    "large_geomean": large_geomean,
                    "small_geomean": "1.0",
                    "benchmarks_sha": "bench-sha",
                    "artifact_digest": "artifact-sha",
                    "gpu_model": "B200",
                    "cuda_version": "13.2",
                    "scoring_schema_version": "1",
                    "per_workload_file": str(per_workload),
                }
            )
        report = root / "report.json"
        report.write_text(json.dumps({"ledger_row": 1, "ledger_path": str(ledger)}))
        task_path = root / "task.json"
        task_path.write_text(
            json.dumps(
                {
                    "schema": "kernelinfra.task.v1",
                    "task_id": "kda-import",
                    "workloads": ["large-0", "small-0"],
                    "comparison": {
                        "primary_workloads": ["large-0", "small-0"],
                        "relative_noise_floor": 0.05,
                    },
                    "stages": [
                        {
                            "id": "authoritative",
                            "kind": "judge",
                            "execution": "local",
                            "judge": {
                                "identity": "KDA@judge-commit",
                                "cwd": str(root),
                                "command": ["kernelinfra-kda-import", "--help"],
                            },
                        }
                    ],
                }
            )
        )
        args = argparse.Namespace(
            report_ref=report,
            expected_kernel="kda_forward",
            expected_benchmarks_sha="bench-sha",
            expected_scoring_schema="1",
            expected_workloads=2,
            judge_identity="KDA@judge-commit",
        )
        return args, load_task(task_path)

    def test_authoritative_speedup_receipt_is_valid_but_not_frontier_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, task = self.fixture(root)
            stage = kda.import_report(args, root / "stage")
            validate_stage_result(stage, task)
            result = aggregate_run_result(
                task=task,
                run_id="run",
                task_digest=task.digest,
                candidate_digest="candidate",
                stage_results=[("authoritative", "judge", stage)],
            )
        self.assertEqual(stage["validity"], "valid")
        self.assertEqual(stage["workloads"][0]["speedup"], 2.0)
        self.assertFalse(stage["metrics"]["absolute_timing_available"])
        self.assertFalse(stage["metrics"]["provenance_complete"])
        self.assertFalse(result["frontier_eligible"])

    def test_tampered_geomean_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, _task = self.fixture(root, large_geomean="9.0")
            with self.assertRaisesRegex(RuntimeError, "large geomean disagrees"):
                kda.import_report(args, root / "stage")


if __name__ == "__main__":
    unittest.main()
