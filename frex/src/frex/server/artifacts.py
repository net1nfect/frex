from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]+\Z")

_root: Path | None = None


def default_root() -> Path:
    base = os.environ.get("FREX_DATA_DIR") or Path.home() / ".local" / "share" / "frex"
    return Path(base) / "artifacts"


def configure(root: Path | None) -> None:
    """Override the artifacts root (used by tests)."""
    global _root
    _root = root


def _base() -> Path:
    return _root or default_root()


def _validated_id(kind: str, value: str) -> str:
    if not _ID_PATTERN.match(value):
        raise ValueError(f"invalid {kind}: {value!r}")
    return value


def run_dir(project_id: str, run_id: str) -> Path:
    """Directory holding one run's artifacts.

    Ids come from the URL and are interpolated into a filesystem path, so
    they are validated first — a traversal attempt must never reach
    ``Path`` arithmetic.
    """
    _validated_id("project_id", project_id)
    _validated_id("run_id", run_id)
    path = _base() / project_id / run_id
    resolved = path.resolve()
    base_resolved = _base().resolve()
    if not str(resolved).startswith(str(base_resolved) + os.sep):
        raise ValueError(f"artifact path escapes the artifacts root: {path}")
    return resolved


def write_run_outputs(
    project_id: str,
    run_id: str,
    stdout: str | None,
    stderr: str | None,
) -> tuple[str | None, str | None]:
    """Persist stdout/stderr for one run; returns the stored paths."""
    directory = run_dir(project_id, run_id)
    stdout_path = stderr_path = None
    if stdout is None and stderr is None:
        return None, None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if stdout is not None:
            stdout_path = str(directory / "stdout.txt")
            (directory / "stdout.txt").write_text(stdout, encoding="utf-8")
        if stderr is not None:
            stderr_path = str(directory / "stderr.txt")
            (directory / "stderr.txt").write_text(stderr, encoding="utf-8")
    except OSError:
        # A failed artifact write must not fail run recording; the database
        # row keeps the full content anyway.
        cleanup_run(project_id, run_id)
        return None, None
    return stdout_path, stderr_path


def cleanup_run(project_id: str, run_id: str) -> None:
    try:
        directory = run_dir(project_id, run_id)
    except ValueError:
        return
    shutil.rmtree(directory, ignore_errors=True)


def remove_project(project_id: str) -> None:
    """Remove every artifact belonging to a deleted project."""
    _validated_id("project_id", project_id)
    shutil.rmtree(_base() / project_id, ignore_errors=True)
