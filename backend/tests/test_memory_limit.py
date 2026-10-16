from types import SimpleNamespace

import pytest

from app.core import memory_limit


def test_memory_budget_is_capped_by_remaining_cgroup_memory(monkeypatch) -> None:
    monkeypatch.setattr(memory_limit, "available_memory_bytes", lambda: 20_000_000_000)
    monkeypatch.setattr(
        memory_limit, "_current_cgroup_remaining_bytes", lambda: 5_000_000_000
    )

    assert memory_limit.memory_budget_bytes() == 5_000_000_000


def test_cgroup_remaining_memory_ignores_reclaimable_file_cache(tmp_path, monkeypatch) -> None:
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "memory.stat").write_text("anon 4000000000\ninactive_file 5000000000\n")
    monkeypatch.setattr(
        memory_limit, "_current_cgroup_memory_bytes", lambda: (10_000_000_000, 9_000_000_000)
    )
    monkeypatch.setattr(memory_limit, "_memory_cgroup_path", lambda: cgroup)

    assert memory_limit._current_cgroup_remaining_bytes() == 6_000_000_000


def test_runner_rechecks_current_budget_inside_api_scope(monkeypatch) -> None:
    monkeypatch.setenv(memory_limit._REEXEC_MARKER, "1")
    monkeypatch.setenv(memory_limit._REEXEC_LIMIT_MARKER, "12000000000")
    monkeypatch.delenv(memory_limit._RUNNER_REEXEC_MARKER, raising=False)
    monkeypatch.setattr(memory_limit, "_current_memory_limit_bytes", lambda: 12_000_000_000)
    monkeypatch.setattr(memory_limit, "memory_budget_bytes", lambda: 4_000_000_000)
    monkeypatch.setattr(memory_limit.shutil, "which", lambda _name: "/usr/bin/systemd-run")
    invoked: dict[str, object] = {}

    def fake_run(command: list[str], *, check: bool) -> SimpleNamespace:
        invoked["command"] = command
        invoked["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(memory_limit.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as result:
        memory_limit.enforce_memory_limit()

    assert result.value.code == 0
    command = invoked["command"]
    assert isinstance(command, list)
    assert "--property=MemoryMax=4000000000" in command
    assert f"--setenv={memory_limit._RUNNER_REEXEC_MARKER}=1" in command


def test_runner_reexec_checks_actual_cgroup_limit(monkeypatch) -> None:
    monkeypatch.setenv(memory_limit._RUNNER_REEXEC_MARKER, "1")
    monkeypatch.setenv(memory_limit._REEXEC_LIMIT_MARKER, "4000000000")
    monkeypatch.setattr(memory_limit, "_current_memory_limit_bytes", lambda: 4_000_000_000)

    assert memory_limit.enforce_memory_limit() == 4_000_000_000


def test_api_scope_uses_fixed_hard_cap_so_job_preflight_can_recheck_memory(monkeypatch) -> None:
    monkeypatch.delenv(memory_limit._REEXEC_MARKER, raising=False)
    monkeypatch.delenv(memory_limit._RUNNER_REEXEC_MARKER, raising=False)
    monkeypatch.setattr(memory_limit, "_current_memory_limit_bytes", lambda: None)
    monkeypatch.setattr(memory_limit, "memory_budget_bytes", lambda: 2_000_000_000)
    monkeypatch.setattr(memory_limit.shutil, "which", lambda _name: "/usr/bin/systemd-run")
    invoked: dict[str, object] = {}

    def fake_run(command: list[str], *, check: bool) -> SimpleNamespace:
        invoked["command"] = command
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(memory_limit.subprocess, "run", fake_run)
    with pytest.raises(SystemExit):
        memory_limit.enforce_memory_limit(
            command=["uvicorn"], use_available_budget=False
        )

    command = invoked["command"]
    assert isinstance(command, list)
    assert f"--property=MemoryMax={memory_limit.MAX_MEMORY_BYTES}" in command
