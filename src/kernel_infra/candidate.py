"""Create self-contained, deterministic candidate snapshots."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

IGNORED_NAMES = frozenset({".git", "__pycache__", ".pytest_cache"})


class CandidateError(ValueError):
    """A candidate cannot be represented as a safe bounded snapshot."""


def validate_candidate(source: Path) -> Path:
    expanded = source.expanduser()
    absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if absolute.is_symlink():
        raise CandidateError("candidate root must not be a symlink")
    source = absolute.resolve()
    if not source.exists():
        raise CandidateError(f"candidate not found: {source}")
    if source.is_file():
        return source
    if not source.is_dir():
        raise CandidateError(f"candidate must be a file or directory: {source}")
    for root_text, directories, files in os.walk(source, followlinks=False):
        root = Path(root_text)
        directories[:] = sorted(
            name for name in directories if name not in IGNORED_NAMES
        )
        for name in directories:
            child = root / name
            if child.is_symlink():
                raise CandidateError(f"candidate contains symlink: {child}")
        for name in files:
            if name in IGNORED_NAMES:
                continue
            child = root / name
            if child.is_symlink():
                raise CandidateError(f"candidate contains symlink: {child}")
            if not child.is_file():
                raise CandidateError(f"candidate contains special file: {child}")
    return source


def snapshot_candidate(source: Path, destination: Path) -> str:
    source = validate_candidate(source)
    destination.mkdir(parents=True, exist_ok=False)
    if source.is_file():
        shutil.copy2(source, destination / source.name)
    elif source.is_dir():
        _copy_directory(source, destination)
    return hash_snapshot(destination)


def _copy_directory(source: Path, destination: Path) -> None:
    for root_text, directories, files in os.walk(source, followlinks=False):
        root = Path(root_text)
        directories[:] = sorted(
            name for name in directories if name not in IGNORED_NAMES
        )
        relative_root = root.relative_to(source)
        target_root = destination / relative_root
        target_root.mkdir(parents=True, exist_ok=True)
        for name in sorted(directories):
            child = root / name
            if child.is_symlink():
                raise CandidateError(f"candidate contains symlink: {child}")
        for name in sorted(files):
            child = root / name
            if name in IGNORED_NAMES:
                continue
            if child.is_symlink():
                raise CandidateError(f"candidate contains symlink: {child}")
            if not child.is_file():
                raise CandidateError(f"candidate contains special file: {child}")
            shutil.copy2(child, target_root / name)


def hash_snapshot(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    for path in files:
        if path.is_symlink():
            raise CandidateError(f"snapshot contains symlink: {path}")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()
