from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.persistence.models.experiment_run import ExperimentRun

SessionFactory = Callable[[], Session]
PROGRESS_PATTERN = re.compile(r"\[(\d+)/(\d+)\]")
MAX_LOG_TAIL_CHARS = 8_000


class LocalExperimentWorker:
    """Run one experiment subprocess at a time and persist its lifecycle."""

    def __init__(
        self,
        session_factory: SessionFactory,
        project_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._project_root = project_root.resolve()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt-experiment")
        self._lock = threading.Lock()
        self._processes: dict[UUID, subprocess.Popen[str]] = {}
        self._submitted: set[UUID] = set()
        self._cancelled: set[UUID] = set()
        self._started = False
        self._closing = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True

        queued_ids: list[UUID] = []
        orphan_pids: list[int] = []
        with self._session_factory() as session:
            runs = list(
                session.scalars(
                    select(ExperimentRun).where(
                        ExperimentRun.status.in_(("queued", "running"))
                    )
                )
            )
            now = datetime.now(UTC)
            for run in runs:
                if run.status == "running":
                    if run.worker_pid is not None:
                        orphan_pids.append(run.worker_pid)
                    run.status = "interrupted"
                    run.finished_at = now
                    run.progress_stage = "Interrompido após reinício; retome criando um novo job"
                    run.error_summary = "O processo da API reiniciou durante a execução"
                    run.worker_pid = None
                else:
                    queued_ids.append(run.id)
            session.commit()

        for pid in orphan_pids:
            self._terminate_orphan(pid)
        for run_id in queued_ids:
            self.enqueue(run_id)

    def enqueue(self, run_id: UUID) -> None:
        with self._lock:
            if self._closing or run_id in self._submitted:
                return
            self._submitted.add(run_id)
        self._executor.submit(self._execute, run_id)

    def cancel(self, run_id: UUID) -> str | None:
        with self._session_factory() as session:
            run = session.get(ExperimentRun, run_id)
            if run is None:
                return None
            status = run.status
            if status == "queued":
                run.status = "interrupted"
                run.finished_at = datetime.now(UTC)
                run.progress_stage = "Interrompido antes de iniciar"
                session.commit()
                return run.status
            if status != "running":
                return status
            run.status = "interrupted"
            run.finished_at = datetime.now(UTC)
            run.progress_stage = "Interrompido pelo operador"
            session.commit()
            with self._lock:
                self._cancelled.add(run_id)
                process = self._processes.get(run_id)
            if process is not None:
                self._terminate_process(process)
            return "interrupted"

    def shutdown(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            active = list(self._processes.items())
            self._cancelled.update(run_id for run_id, _process in active)
        for _run_id, process in active:
            self._terminate_process(process)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _execute(self, run_id: UUID) -> None:
        with self._session_factory() as session:
            run = session.get(ExperimentRun, run_id)
            if run is None or run.status != "queued":
                self._forget(run_id)
                return
            parameters = dict(run.parameters)
            job_type = run.job_type
            run.status = "running"
            run.started_at = datetime.now(UTC)
            run.progress_percent = max(run.progress_percent, 1)
            run.progress_stage = "Preparando o runner"
            run.error_summary = None
            session.commit()

        output_path = self._project_root / "data/processed/jobs" / f"{run_id}.json"
        preview_path = output_path.with_suffix(".preview.json")
        route_paths_path = output_path.with_suffix(".paths.sqlite")
        route_paths_tmp_path = route_paths_path.with_name(route_paths_path.name + ".tmp")
        preview_path.unlink(missing_ok=True)
        route_paths_path.unlink(missing_ok=True)
        route_paths_tmp_path.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        logs: list[str] = []
        last_persisted = 0.0
        last_progress = 1
        process: subprocess.Popen[str] | None = None
        preserve_preview = False
        preserve_route_paths = False
        try:
            command = self._build_command(job_type, parameters, output_path)
            if job_type in ("tsp", "mtsp", "overlap"):
                command.extend(("--preview-path", str(preview_path)))
            process = subprocess.Popen(
                command,
                cwd=self._project_root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            with self._lock:
                self._processes[run_id] = process
                should_stop = run_id in self._cancelled or self._closing
            with self._session_factory() as session:
                run = session.get(ExperimentRun, run_id)
                if run is not None and run.status == "running":
                    run.worker_pid = process.pid
                    session.commit()
            if should_stop:
                self._terminate_process(process)

            if process.stdout is None:
                raise RuntimeError("Não foi possível capturar a saída do runner")
            overlap_phase_two = False
            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                if job_type == "overlap" and line.startswith("D6:"):
                    overlap_phase_two = True
                logs.append(line)
                logs = logs[-80:]
                if job_type == "overlap":
                    progress = self._progress_from_line(
                        line,
                        last_progress,
                        progress_start=45 if overlap_phase_two else 1,
                        progress_span=50 if overlap_phase_two else 44,
                    )
                else:
                    progress = self._progress_from_line(line, last_progress)
                now = datetime.now(UTC).timestamp()
                if progress != last_progress or now - last_persisted >= 1.0:
                    self._persist_progress(run_id, progress, line, logs)
                    last_progress = progress
                    last_persisted = now
            return_code = process.wait()
            with self._lock:
                was_interrupted = run_id in self._cancelled or self._closing

            if was_interrupted:
                self._finish(
                    run_id,
                    status="interrupted",
                    stage="Execução interrompida",
                    logs=logs,
                    progress=last_progress,
                )
            elif return_code == 0 and output_path.is_file():
                summary = self._read_result_summary(output_path)
                self._finish(
                    run_id,
                    status="succeeded",
                    stage="Concluído",
                    logs=logs,
                    progress=100,
                    result_path=str(output_path.relative_to(self._project_root)),
                    result_summary=summary,
                )
                preserve_preview = preview_path.is_file()
                preserve_route_paths = job_type == "overlap" and route_paths_path.is_file()
            else:
                error = next(
                    (
                        line.strip()
                        for line in reversed(logs)
                        if any(
                            message in line
                            for message in (
                                "Memória livre insuficiente",
                                "systemd-run indisponível",
                                "systemd não aplicou o teto",
                            )
                        )
                    ),
                    f"Runner terminou com código {return_code}",
                )
                self._finish(
                    run_id,
                    status="failed",
                    stage="Falha na execução",
                    logs=logs,
                    progress=last_progress,
                    error_summary=error,
                )
        except Exception as error:
            logs.append(str(error))
            self._finish(
                run_id,
                status="failed",
                stage="Falha ao iniciar o runner",
                logs=logs[-80:],
                progress=last_progress,
                error_summary=str(error)[:2_000],
            )
        finally:
            checkpoint_path = output_path.with_suffix(".checkpoint.json")
            base_report_path = output_path.with_name(f"{output_path.stem}.d5-base.json")
            base_checkpoint_path = base_report_path.with_suffix(".checkpoint.json")
            temporary_paths = (
                preview_path.with_name(preview_path.name + ".tmp"),
                output_path.with_name(output_path.name + ".tmp"),
                checkpoint_path,
                checkpoint_path.with_name(checkpoint_path.name + ".tmp"),
                base_report_path,
                base_report_path.with_name(base_report_path.name + ".tmp"),
                base_checkpoint_path,
                base_checkpoint_path.with_name(base_checkpoint_path.name + ".tmp"),
            )
            if not preserve_preview:
                temporary_paths = (preview_path, *temporary_paths)
            if not preserve_route_paths:
                temporary_paths = (
                    route_paths_path,
                    route_paths_tmp_path,
                    *temporary_paths,
                )
            for temporary_path in temporary_paths:
                temporary_path.unlink(missing_ok=True)
            if process is not None:
                with self._lock:
                    self._processes.pop(run_id, None)
            self._forget(run_id)

    def _build_command(
        self,
        job_type: str,
        parameters: dict[str, Any],
        output_path: Path,
    ) -> list[str]:
        scripts = {
            "tsp": "d4_open_tsp.py",
            "mtsp": "d5_open_mtsp.py",
            "overlap": "d6_custom.py",
        }
        try:
            script = scripts[job_type]
        except KeyError as error:
            raise ValueError(f"Tipo de job desconhecido: {job_type}") from error
        command = [
            sys.executable,
            str(self._project_root / "experiments/runners" / script),
            "--time-limit-s",
            str(parameters["time_limit_s"]),
            "--output",
            str(output_path),
        ]
        command.extend(("--repetition-policy", str(parameters.get("repetition_policy", "allow"))))
        if job_type in ("mtsp", "overlap"):
            command.extend(
                ("--workers", str(parameters["workers"]), "--seed", str(parameters["seed"]))
            )
        if job_type == "tsp":
            command.extend(("--seed", str(parameters.get("seed", 20261008))))
            command.extend(
                (
                    "--objective", str(parameters.get("objective", "distance")),
                    "--origin-mode", "fixed" if parameters.get("origin_ibge_code") else "auto",
                )
            )
            if parameters.get("origin_ibge_code") is not None:
                command.extend(("--origin-ibge-code", str(parameters["origin_ibge_code"])))
            if parameters.get("destination_ibge_code") is not None:
                command.extend(("--destination-ibge-code", str(parameters["destination_ibge_code"])))
        if job_type == "mtsp":
            command.extend(
                (
                    "--objective", str(parameters.get("objective", "distance")),
                    "--vehicle-count", str(parameters.get("vehicle_count", 2)),
                    "--origin-mode", "fixed" if parameters.get("origin_ibge_code") else "auto",
                )
            )
            if parameters.get("origin_ibge_code") is not None:
                command.extend(("--origin-ibge-code", str(parameters["origin_ibge_code"])))
        if job_type == "overlap":
            command.extend(("--variants", str(parameters["variants"])))
            command.extend(("--lambdas", *(str(value) for value in parameters["lambdas"])))
            command.extend(
                (
                    "--objective", str(parameters.get("objective", "distance")),
                    "--vehicle-count", str(parameters.get("vehicle_count", 2)),
                    "--origin-mode", "fixed" if parameters.get("origin_ibge_code") else "auto",
                )
            )
            if parameters.get("origin_ibge_code") is not None:
                command.extend(("--origin-ibge-code", str(parameters["origin_ibge_code"])))
        return command

    def _persist_progress(
        self,
        run_id: UUID,
        progress: int,
        stage: str,
        logs: list[str],
    ) -> None:
        with self._session_factory() as session:
            run = session.get(ExperimentRun, run_id)
            if run is None or run.status != "running":
                return
            run.progress_percent = max(run.progress_percent, min(progress, 95))
            run.progress_stage = stage[:240]
            run.log_tail = "\n".join(logs)[-MAX_LOG_TAIL_CHARS:]
            session.commit()

    def _finish(
        self,
        run_id: UUID,
        *,
        status: str,
        stage: str,
        logs: list[str],
        progress: int,
        result_path: str | None = None,
        result_summary: dict[str, Any] | None = None,
        error_summary: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            run = session.get(ExperimentRun, run_id)
            if run is None:
                return
            run.status = status
            run.progress_percent = 100 if status == "succeeded" else min(progress, 99)
            run.progress_stage = stage
            run.finished_at = datetime.now(UTC)
            run.log_tail = "\n".join(logs)[-MAX_LOG_TAIL_CHARS:]
            run.result_path = result_path
            run.result_summary = result_summary
            run.error_summary = error_summary
            run.worker_pid = None
            session.commit()

    @staticmethod
    def _progress_from_line(
        line: str,
        current: int,
        *,
        progress_start: int = 5,
        progress_span: int = 90,
    ) -> int:
        match = PROGRESS_PATTERN.search(line)
        if match is None:
            return current
        completed, total = int(match.group(1)), int(match.group(2))
        if total <= 0:
            return current
        return max(current, min(95, progress_start + int(progress_span * completed / total)))

    @staticmethod
    def _read_result_summary(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        scenarios = payload.get("scenarios")
        if not isinstance(scenarios, list):
            return {"format_version": payload.get("format_version")}
        counts: dict[str, int] = {}
        for scenario in scenarios:
            status = str(scenario.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        return {
            "scenario_count": len(scenarios),
            "status_counts": counts,
            "city_count": payload.get("city_count"),
        }

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def _terminate_orphan(self, pid: int) -> None:
        command_line_path = Path(f"/proc/{pid}/cmdline")
        try:
            command_line = command_line_path.read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", errors="replace"
            )
        except OSError:
            return
        if str(self._project_root) not in command_line or "/experiments/runners/" not in command_line:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def _forget(self, run_id: UUID) -> None:
        with self._lock:
            self._processes.pop(run_id, None)
            self._submitted.discard(run_id)
            self._cancelled.discard(run_id)
