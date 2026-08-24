import json
import tempfile
import unittest
from pathlib import Path

from kernel_infra.contracts import load_task
from kernel_infra.frontier import rebuild_frontier
from kernel_infra.store import RunStore

from test_contracts import task_value


class FrontierTests(unittest.TestCase):
    def test_valid_runs_get_separate_frontier_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_path = root / "task.json"
            task_path.write_text(json.dumps(task_value(root)))
            task = load_task(task_path)
            store = RunStore(root / "state")

            first = self._add_run(store, task, root, "first", 1.0, "aaa")
            second = self._add_run(store, task, root, "second", 0.8, "bbb")
            duplicate = self._add_run(store, task, root, "dup", 0.7, "bbb")
            projection = rebuild_frontier(store, task)

            decisions = {
                item["run_id"]: item["decision"] for item in projection["decisions"]
            }
            self.assertEqual(decisions[first], "first-valid")
            self.assertEqual(decisions[second], "improves-primary")
            self.assertEqual(decisions[duplicate], "compiler-equivalent")
            self.assertEqual(projection["cells"]["w0"]["run_id"], second)

    def _add_run(self, store, task, root, label, candidate_ms, cubin):
        candidate = root / f"candidate-{label}"
        candidate.mkdir()
        (candidate / "kernel.py").write_text(f"# {label}\n")
        state = store.create_run(task=task, candidate=candidate, label=label)
        result = {
            "schema": "kernelinfra.run-result.v1",
            "run_id": state["run_id"],
            "task_id": task.task_id,
            "task_sha256": task.digest,
            "candidate_sha256": state["candidate_sha256"],
            "outcome": "completed",
            "validity": "valid",
            "frontier_eligible": True,
            "terminal_reason": None,
            "stages": [],
            "workloads": [
                {
                    "id": "w0",
                    "correct": True,
                    "candidate_ms": candidate_ms,
                    "baseline_ms": 2.0,
                    "stable": True,
                }
            ],
            "fingerprints": {"cubin_sha256": cubin},
        }
        store.atomic_json(store.run_dir(state["run_id"]) / "result.json", result)
        store.update_state(state["run_id"], "completed", state="completed")
        return state["run_id"]


if __name__ == "__main__":
    unittest.main()
