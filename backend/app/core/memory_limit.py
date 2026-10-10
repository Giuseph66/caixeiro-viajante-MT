"""Run expensive local work inside a hard cgroup memory limit."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

MAX_MEMORY_BYTES = 25_000_000_000
_REEXEC_MARKER = "MT_MEMORY_LIMIT_REEXEC"


def _current_memory_limit_bytes() -> int | None:
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            hierarchy, _controllers, group_path = line.split(":", 2)
            if hierarchy != "0":
                continue
            limit_path = Path("/sys/fs/cgroup") / group_path.lstrip("/") / "memory.max"
            value = limit_path.read_text().strip()
            return int(value) if value.isdecimal() else None
    except (OSError, ValueError):
        return None
    return None


def enforce_memory_limit(
    max_bytes: int = MAX_MEMORY_BYTES,
    *,
    command: Sequence[str] | None = None,
) -> int:
    """Re-exec once in a dedicated systemd scope with a hard memory cap."""
    current_limit = _current_memory_limit_bytes()
    if os.environ.get(_REEXEC_MARKER) == "1":
        if current_limit is None or current_limit > max_bytes:
            raise RuntimeError("systemd não aplicou o teto de memória solicitado")
        return current_limit

    systemd_run = shutil.which("systemd-run")
    if systemd_run is None:
        raise RuntimeError("systemd-run indisponível; cálculo bloqueado sem teto de memória")

    child_command = list(command) if command is not None else [
        sys.executable,
        str(Path(sys.argv[0]).resolve()),
        *sys.argv[1:],
    ]
    completed = subprocess.run(
        [
            systemd_run,
            "--user",
            "--scope",
            "--collect",
            "--quiet",
            "--same-dir",
            f"--property=MemoryMax={max_bytes}",
            f"--setenv={_REEXEC_MARKER}=1",
            "--",
            *child_command,
        ],
        check=False,
    )
    raise SystemExit(completed.returncode)
