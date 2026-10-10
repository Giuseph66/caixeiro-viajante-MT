from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.persistence.models.experiment_run import ExperimentRun


def create_run(
    session: Session,
    *,
    job_type: str,
    parameters: dict[str, Any],
    data_manifest_sha256: str,
    seed: int,
    algorithm_version: str,
) -> ExperimentRun:
    run = ExperimentRun(
        job_type=job_type,
        status="queued",
        parameters=parameters,
        progress_percent=0,
        progress_stage="Na fila",
        data_manifest_sha256=data_manifest_sha256,
        seed=seed,
        algorithm_version=algorithm_version,
    )
    session.add(run)
    session.flush()
    return run


def get_run(session: Session, run_id: UUID) -> ExperimentRun | None:
    return session.get(ExperimentRun, run_id)


def list_runs(session: Session, *, limit: int, offset: int) -> list[ExperimentRun]:
    statement = (
        select(ExperimentRun)
        .order_by(ExperimentRun.created_at.desc(), ExperimentRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(statement))


def count_by_status(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(ExperimentRun.status, func.count()).group_by(ExperimentRun.status)
    )
    return {status: count for status, count in rows}
