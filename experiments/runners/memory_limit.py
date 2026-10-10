"""Start experiment runners inside a hard cgroup memory limit."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

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


def enforce_memory_limit(max_bytes: int = MAX_MEMORY_BYTES) -> int:
    """Re-exec this runner in systemd if its current cgroup is not capped."""
    current_limit = _current_memory_limit_bytes()
    if current_limit is not None and current_limit <= max_bytes:
        return current_limit

    if os.environ.get(_REEXEC_MARKER) == "1":
        raise RuntimeError("systemd não aplicou o teto de memória solicitado")

    systemd_run = shutil.which("systemd-run")
    if systemd_run is None:
        raise RuntimeError("systemd-run indisponível; cálculo bloqueado sem teto de memória")

    runner = Path(sys.argv[0]).resolve()
    command = [
        systemd_run,
        "--user",
        "--scope",
        "--collect",
        "--quiet",
        "--same-dir",
        f"--property=MemoryMax={max_bytes}",
        f"--setenv={_REEXEC_MARKER}=1",
        "--",
        sys.executable,
        str(runner),
        *sys.argv[1:],
    ]
    completed = subprocess.run(command, check=False)
    raise SystemExit(completed.returncode)
