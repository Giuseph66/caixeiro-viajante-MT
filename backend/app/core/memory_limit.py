"""Run expensive local work inside a hard cgroup memory limit."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

MAX_MEMORY_BYTES = 25_000_000_000
MEMORY_BUDGET_RATIO = 0.8
MINIMUM_MEMORY_BUDGET_BYTES = 1_000_000_000
_REEXEC_MARKER = "MT_MEMORY_LIMIT_REEXEC"
_REEXEC_LIMIT_MARKER = "MT_MEMORY_LIMIT_BYTES"
_RUNNER_REEXEC_MARKER = "MT_MEMORY_LIMIT_RUNNER_REEXEC"


def available_memory_bytes() -> int:
    """Return Linux's estimate of memory that can be allocated without swapping."""
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("Não foi possível ler MemAvailable em /proc/meminfo")


def memory_budget_bytes(available_bytes: int | None = None) -> int:
    available = available_memory_bytes() if available_bytes is None else available_bytes
    if available < 0:
        raise ValueError("Memória disponível não pode ser negativa")
    budget = min(MAX_MEMORY_BYTES, int(available * MEMORY_BUDGET_RATIO))
    cgroup_remaining = _current_cgroup_remaining_bytes()
    return min(budget, cgroup_remaining) if cgroup_remaining is not None else budget


def _memory_cgroup_path() -> Path | None:
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            hierarchy, _controllers, group_path = line.split(":", 2)
            if hierarchy != "0":
                continue
            return Path("/sys/fs/cgroup") / group_path.lstrip("/")
    except (OSError, ValueError):
        return None
    return None


def _current_cgroup_memory_bytes() -> tuple[int | None, int | None]:
    cgroup_path = _memory_cgroup_path()
    if cgroup_path is None:
        return None, None
    try:
        limit = cgroup_path.joinpath("memory.max").read_text().strip()
        current = cgroup_path.joinpath("memory.current").read_text().strip()
        return int(limit) if limit.isdecimal() else None, int(current)
    except (OSError, ValueError):
        return None, None


def _current_memory_limit_bytes() -> int | None:
    return _current_cgroup_memory_bytes()[0]


def _current_cgroup_remaining_bytes() -> int | None:
    limit, current = _current_cgroup_memory_bytes()
    if limit is None or current is None:
        return None
    inactive_file = 0
    cgroup_path = _memory_cgroup_path()
    if cgroup_path is not None:
        try:
            for line in cgroup_path.joinpath("memory.stat").read_text().splitlines():
                key, value = line.split(maxsplit=1)
                if key == "inactive_file":
                    inactive_file = int(value)
                    break
        except (OSError, ValueError):
            pass
    reclaimable = min(current, max(0, inactive_file))
    return max(0, limit - (current - reclaimable))


def enforce_memory_limit(
    max_bytes: int | None = None,
    *,
    command: Sequence[str] | None = None,
    use_available_budget: bool = True,
) -> int:
    """Re-exec once in a dedicated systemd scope with a hard memory cap."""
    current_limit = _current_memory_limit_bytes()
    if os.environ.get(_RUNNER_REEXEC_MARKER) == "1":
        inherited_limit = int(os.environ.get(_REEXEC_LIMIT_MARKER, MAX_MEMORY_BYTES))
        if current_limit is None or current_limit > inherited_limit:
            raise RuntimeError("systemd não aplicou o teto de memória solicitado")
        return current_limit

    inherited_limit = MAX_MEMORY_BYTES
    if os.environ.get(_REEXEC_MARKER) == "1":
        inherited_limit = int(os.environ.get(_REEXEC_LIMIT_MARKER, MAX_MEMORY_BYTES))
        if current_limit is None or current_limit > inherited_limit:
            raise RuntimeError("systemd não aplicou o teto de memória da API")

    budget = memory_budget_bytes() if use_available_budget else MAX_MEMORY_BYTES
    requested_limit = min(max_bytes or MAX_MEMORY_BYTES, budget, inherited_limit)

    if requested_limit < MINIMUM_MEMORY_BUDGET_BYTES:
        raise RuntimeError(
            "Memória livre insuficiente para iniciar o cálculo com segurança "
            f"({requested_limit / 1_000_000_000:.2f} GB de limite)."
        )

    systemd_run = shutil.which("systemd-run")
    if systemd_run is None:
        raise RuntimeError("systemd-run indisponível; cálculo bloqueado sem teto de memória")

    child_command = list(command) if command is not None else [
        sys.executable,
        str(Path(sys.argv[0]).resolve()),
        *sys.argv[1:],
    ]
    command_args = [
        systemd_run,
        "--user",
        "--scope",
        "--collect",
        "--quiet",
        "--same-dir",
        f"--property=MemoryMax={requested_limit}",
        f"--setenv={_REEXEC_MARKER}=1",
        f"--setenv={_REEXEC_LIMIT_MARKER}={requested_limit}",
    ]
    if command is None:
        command_args.append(f"--setenv={_RUNNER_REEXEC_MARKER}=1")
    command_args.extend(("--", *child_command))
    completed = subprocess.run(
        command_args,
        check=False,
    )
    raise SystemExit(completed.returncode)
