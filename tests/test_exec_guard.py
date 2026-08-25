import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "src" / "kernel_infra" / "exec_guard.py"


class ExecGuardTests(unittest.TestCase):
    def test_lease_loss_terminates_and_reaps_child_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "child.pid"
            child_path = root / "child.py"
            child_path.write_text(
                "import os, pathlib, time\n"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            read_fd, write_fd = os.pipe()
            guard = subprocess.Popen(
                [
                    sys.executable,
                    str(GUARD),
                    "--lease-fd",
                    str(read_fd),
                    "--grace-seconds",
                    "0.2",
                    "--",
                    sys.executable,
                    str(child_path),
                ],
                pass_fds=(read_fd,),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            os.close(read_fd)
            deadline = time.monotonic() + 5
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_path.exists())
            child_pid = int(pid_path.read_text())
            os.close(write_fd)
            _stdout, stderr = guard.communicate(timeout=5)
            self.assertEqual(guard.returncode, 130)
            self.assertIn(b"parent lease lost", stderr)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, signal.SIGCONT)

    def test_child_exit_code_is_preserved(self):
        read_fd, write_fd = os.pipe()
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(GUARD),
                    "--lease-fd",
                    str(read_fd),
                    "--",
                    sys.executable,
                    "-c",
                    "raise SystemExit(7)",
                ],
                pass_fds=(read_fd,),
                check=False,
            )
            self.assertEqual(completed.returncode, 7)
        finally:
            os.close(read_fd)
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
