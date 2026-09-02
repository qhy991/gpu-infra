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
import stat
import subprocess
import sys
import time
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kernelinfra-exec-guard")
    parser.add_argument("--lease-fd", type=int)
    parser.add_argument("--grace-seconds", type=float, default=5.0)
    parser.add_argument("--finalize-dir", type=Path)
    parser.add_argument("--dir-mode", type=lambda value: int(value, 8))
    parser.add_argument("--file-mode", type=lambda value: int(value, 8))
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


def _finalize_owned_tree(root: Path, *, dir_mode: int, file_mode: int) -> None:
    root = root.resolve(strict=True)
    owner = os.geteuid()
    for directory, names, files in os.walk(root, topdown=False, followlinks=False):
        base = Path(directory)
        for name in files:
            path = base / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError(f"stage artifact is not a regular file: {path}")
            if metadata.st_uid == owner:
                path.chmod(file_mode)
        for name in names:
            path = base / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(f"stage artifact is not a directory: {path}")
            if metadata.st_uid == owner:
                path.chmod(dir_mode)


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
    finalize_modes = (args.dir_mode, args.file_mode)
    if args.finalize_dir is None and finalize_modes != (None, None):
        print("kernelinfra-exec-guard: modes require --finalize-dir", file=sys.stderr)
        return 2
    if args.finalize_dir is not None and None in finalize_modes:
        print("kernelinfra-exec-guard: finalization requires both modes", file=sys.stderr)
        return 2
    if any(mode is not None and not 0 <= mode <= 0o777 for mode in finalize_modes):
        print("kernelinfra-exec-guard: invalid finalization mode", file=sys.stderr)
        return 2

    requested_signal: list[int] = []

    def request_stop(signum: int, _frame: object) -> None:
        if not requested_signal:
            requested_signal.append(signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)

    child = subprocess.Popen(command, start_new_session=True, close_fds=True)
    selector = selectors.DefaultSelector() if args.lease_fd is not None else None
    if selector is not None:
        selector.register(args.lease_fd, selectors.EVENT_READ)
    returncode = 1
    try:
        while True:
            returncode = child.poll()
            if returncode is not None:
                break
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
                returncode = 128 + signum
                break
            if selector is None:
                time.sleep(0.1)
                continue
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
                returncode = 130
                break
            if returncode == 130:
                break
    finally:
        if selector is not None:
            selector.close()
        if args.lease_fd is not None:
            try:
                os.close(args.lease_fd)
            except OSError:
                pass
    if args.finalize_dir is not None:
        try:
            _finalize_owned_tree(
                args.finalize_dir,
                dir_mode=args.dir_mode,
                file_mode=args.file_mode,
            )
        except (OSError, RuntimeError) as exc:
            _note(f"[kernel-exec-guard] stage artifact finalization failed: {exc}")
            return 1
    return returncode if returncode >= 128 else _exit_code(returncode)


if __name__ == "__main__":
    raise SystemExit(main())
