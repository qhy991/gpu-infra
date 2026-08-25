import importlib.util
import json
import unittest
from pathlib import Path

from kernel_infra.contracts import load_task


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "a800_cuda_smoke"
RMSNORM = ROOT / "examples" / "a800_rmsnorm_smoke"


def load_evaluator():
    path = ROOT / "src" / "kernel_infra" / "adapters" / "cuda_container.py"
    spec = importlib.util.spec_from_file_location("a800_cuda_evaluator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CudaContainerEvaluatorTests(unittest.TestCase):
    def test_task_binds_evaluator_harness_and_image(self):
        evaluator = load_evaluator()
        bundle = evaluator.bundle_sha256(EXAMPLE)
        task = load_task(EXAMPLE / "task.json")
        identities = {stage.judge_identity for stage in task.stages}
        self.assertEqual(len(identities), 1)
        identity = identities.pop()
        self.assertIn(f"sha256:{bundle}", identity)
        self.assertIn(
            "image:sha256:01fb061898c1391f77073da003e3bfa2b92b33679d2e024a932fa9d1ed635cf0",
            identity,
        )
        self.assertEqual(
            [
                (
                    stage.kind,
                    stage.resources.mode if stage.resources is not None else None,
                )
                for stage in task.stages
            ],
            [
                ("compile", None),
                ("correctness", "shared"),
                ("sanitize", "exclusive"),
                ("benchmark", "exclusive"),
            ],
        )

    def test_harness_json_is_extracted_after_container_banner(self):
        evaluator = load_evaluator()
        expected = {"mode": "correctness", "workloads": []}
        text = "CUDA banner\nwarning\n" + json.dumps(expected) + "\n"
        self.assertEqual(evaluator._last_json_object(text), expected)

    def test_rmsnorm_task_reuses_and_binds_canonical_adapter(self):
        evaluator = load_evaluator()
        bundle = evaluator.bundle_sha256(RMSNORM)
        task = load_task(RMSNORM / "task.json")
        self.assertEqual(
            {stage.judge_identity for stage in task.stages},
            {
                "a800-rmsnorm@sha256:"
                + bundle
                + "+image:sha256:01fb061898c1391f77073da003e3bfa2b92b33679d2e024a932fa9d1ed635cf0"
            },
        )
        self.assertEqual(
            list(task.workloads),
            ["rmsnorm-r4096-c1024", "rmsnorm-r2048-c4096"],
        )
        for name in ("candidate_basic", "candidate_warp", "candidate_incorrect"):
            source = (RMSNORM / name / "kernel.cu").read_text(encoding="utf-8")
            self.assertIn('extern "C" void launch_candidate', source)

    def test_candidates_implement_the_frozen_abi(self):
        for name in (
            "candidate_basic",
            "candidate_grid_stride",
            "candidate_incorrect",
            "candidate_oob",
            "candidate_race",
        ):
            source = (EXAMPLE / name / "kernel.cu").read_text(encoding="utf-8")
            self.assertIn('extern "C" void launch_candidate', source)


if __name__ == "__main__":
    unittest.main()
