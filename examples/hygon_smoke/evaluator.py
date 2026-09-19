"""HIP runtime correctness smoke, runnable only inside a Hygon allocation."""
import importlib.util
import json
import os
from pathlib import Path


def main():
    result = {"schema": "kernelinfra.stage-result.v1", "status": "failed", "validity": "unknown"}
    try:
        if os.environ.get("GPUQ_BACKEND") != "hygon":
            raise RuntimeError("Hygon broker allocation required")
        import torch
        if not torch.version.hip or not torch.cuda.is_available():
            raise RuntimeError("HIP-enabled PyTorch required")
        if torch.cuda.device_count() != 1:
            raise RuntimeError("expected exactly one broker-visible HIP device")
        source = Path(os.environ['KERNELINFRA_CANDIDATE_DIR']) / 'kernel.py'
        spec = importlib.util.spec_from_file_location('candidate', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        x = torch.arange(65536, dtype=torch.float32, device='cuda') % 257 - 128
        y = module.run(x)
        torch.cuda.synchronize()
        reference = (torch.arange(65536, dtype=torch.float32) % 257 - 128) * 2
        correct = isinstance(y, torch.Tensor) and y.device == x.device and torch.equal(y.cpu(), reference)
        result.update(status='passed' if correct else 'failed', validity='valid' if correct else 'invalid', workloads=[{'id':'vector-65536','correct':bool(correct)}], metrics={'backend':'hygon','hip_version':torch.version.hip,'device':torch.cuda.get_device_name(0),'physical_device_ids':os.environ['GPUQ_DEVICE_IDS']})
    except Exception as exc:
        result['summary'] = str(exc)
    Path(os.environ['KERNELINFRA_RESULT']).write_text(json.dumps(result))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
