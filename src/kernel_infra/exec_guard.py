"""Run one command under a parent-owned pipe lease.

The guard is intentionally dependency-free and executable by file path. The
daemon holds the pipe's write end. An ungraceful daemon death closes that file
descriptor in the kernel, causing the guard to terminate and reap the complete
child process group.
"""

from __future__ import annotations

import argparse
import os
import selectors
import signal
import subprocess
import sys
import time


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kernelinfra-exec-guard")
    parser.add_argument("--lease-fd", type=int, required=True)
    parser.add_argument("--grace-seconds", type=float, default=5.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _exit_code(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 + abs(returncode)


def _note(message: str) -> None:
    try:
        print(message, file=sys.stderr, flush=True)
    except (BrokenPipeError, OSError):
        pass


def _terminate_group(
    child: subprocess.Popen[bytes], *, first_signal: int, grace_seconds: float
) -> None:
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, first_signal)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        child.wait(timeout=max(1.0, grace_seconds))
    except subprocess.TimeoutExpired:
        pass


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        print("kernelinfra-exec-guard: missing command", file=sys.stderr)
        return 2
    if args.grace_seconds < 0:
        print("kernelinfra-exec-guard: grace must be non-negative", file=sys.stderr)
        return 2

    requested_signal: list[int] = []

    def request_stop(signum: int, _frame: object) -> None:
        if not requested_signal:
            requested_signal.append(signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)

    child = subprocess.Popen(command, start_new_session=True, close_fds=True)
    selector = selectors.DefaultSelector()
    selector.register(args.lease_fd, selectors.EVENT_READ)
    try:
        while True:
            returncode = child.poll()
            if returncode is not None:
                return _exit_code(returncode)
            if requested_signal:
                signum = requested_signal[0]
                _note(
                    f"[kernel-exec-guard] received signal {signum}; "
                    "terminating child"
                )
                _terminate_group(
                    child,
                    first_signal=signum,
                    grace_seconds=args.grace_seconds,
                )
                return 128 + signum
            for key, _mask in selector.select(timeout=0.1):
                data = os.read(key.fd, 1)
                if data:
                    continue
                _note("[kernel-exec-guard] parent lease lost; terminating child")
                _terminate_group(
                    child,
                    first_signal=signal.SIGTERM,
                    grace_seconds=args.grace_seconds,
                )
                return 130
    finally:
        selector.close()
        try:
            os.close(args.lease_fd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
