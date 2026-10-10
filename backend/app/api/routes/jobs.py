from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_job_worker, get_session
from app.core.config import PROJECT_ROOT
from app.optimization.overlap import OVERLAP_ALGORITHM_VERSION
from app.optimization.visitation.mtsp import MTSP_ALGORITHM_VERSION
from app.optimization.visitation.tsp import TSP_ALGORITHM_VERSION
from app.persistence.models.experiment_run import ExperimentRun
from app.persistence.repositories.experiment_runs import create_run, get_run, list_runs
from app.schemas.api import JobCreateRequest, JobResponse
from app.services.experiment_results import (
    build_geojson_file,
    result_file_for_run,
    result_metrics,
    result_routes,
)

router = APIRouter(tags=["experiments"])

ALGORITHM_VERSIONS = {
    "tsp": TSP_ALGORITHM_VERSION,
    "mtsp": MTSP_ALGORITHM_VERSION,
    "overlap": OVERLAP_ALGORITHM_VERSION,
}


@router.post("", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
def create_job(
    payload: JobCreateRequest,
    session: Session = Depends(get_session),
    worker: Any = Depends(get_job_worker),
) -> JobResponse:
    fingerprint = _data_manifest_fingerprint()
    parameters = payload.model_dump(mode="json")
    run = create_run(
        session,
        job_type=payload.job_type,
        parameters=parameters,
        data_manifest_sha256=fingerprint,
        seed=payload.seed,
        algorithm_version=ALGORITHM_VERSIONS[payload.job_type],
    )
    session.commit()
    session.refresh(run)
    try:
        worker.enqueue(run.id)
    except Exception as error:
        run.status = "failed"
        run.progress_stage = "Falha ao enfileirar o job"
        run.error_summary = str(error)[:2_000]
        session.commit()
        raise HTTPException(status_code=503, detail="Não foi possível enfileirar o job") from error
    return _to_response(run)


@router.get("", response_model=list[JobResponse])
def list_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[JobResponse]:
    return [_to_response(run) for run in list_runs(session, limit=limit, offset=offset)]


@router.get("/{run_id}", response_model=JobResponse)
def get_job(run_id: UUID, session: Session = Depends(get_session)) -> JobResponse:
    run = get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    return _to_response(run)


@router.get("/{run_id}/metrics")
def get_job_metrics(run_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    run = _get_completed_run(session, run_id)
    return result_metrics(run)


@router.get("/{run_id}/routes")
def get_job_routes(
    run_id: UUID,
    scenario: str = Query(..., min_length=1, max_length=100),
    policy: Literal["P1", "P2", "P3"] = "P1",
    origin_ibge_code: int | None = Query(default=None, ge=1000000, le=9999999),
    lambda_value: float = Query(default=1.0, ge=0, le=100, alias="lambda"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    run = _get_completed_run(session, run_id)
    routes = result_routes(
        run,
        scenario=scenario,
        policy=policy,
        origin_ibge_code=origin_ibge_code,
        lambda_value=lambda_value,
    )
    return {
        "run_id": str(run.id),
        "scenario": scenario,
        "policy": policy,
        "lambda": lambda_value if policy == "P2" else None,
        "routes": routes,
    }


@router.get("/{run_id}/geojson")
def get_job_geojson(
    run_id: UUID,
    scenario: str = Query(..., min_length=1, max_length=100),
    policy: Literal["P1", "P2", "P3"] = "P1",
    origin_ibge_code: int | None = Query(default=None, ge=1000000, le=9999999),
    lambda_value: float = Query(default=1.0, ge=0, le=100, alias="lambda"),
    session: Session = Depends(get_session),
) -> FileResponse:
    run = _get_completed_run(session, run_id)
    routes = result_routes(
        run,
        scenario=scenario,
        policy=policy,
        origin_ibge_code=origin_ibge_code,
        lambda_value=lambda_value,
    )
    cache_key = f"{scenario}:{policy}:{origin_ibge_code}:{lambda_value if policy == 'P2' else ''}"
    output_path = build_geojson_file(
        run,
        routes,
        cache_key=cache_key,
        project_root=PROJECT_ROOT,
    )
    return FileResponse(output_path, media_type="application/geo+json")


@router.get("/{run_id}/export")
def export_job(run_id: UUID, session: Session = Depends(get_session)) -> FileResponse:
    run = _get_completed_run(session, run_id)
    return FileResponse(
        result_file_for_run(run),
        media_type="application/json",
        filename=f"experimento-{run.id}.json",
    )


@router.post("/{run_id}/cancel", response_model=JobResponse)
def cancel_job(
    run_id: UUID,
    session: Session = Depends(get_session),
    worker: Any = Depends(get_job_worker),
) -> JobResponse:
    resulting_status = worker.cancel(run_id)
    if resulting_status is None:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    run = get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    return _to_response(run)


def _to_response(run: ExperimentRun) -> JobResponse:
    return JobResponse(
        id=run.id,
        job_type=run.job_type,
        status=run.status,
        parameters=run.parameters,
        progress_percent=run.progress_percent,
        progress_stage=run.progress_stage,
        result_available=bool(run.result_path),
        result_summary=run.result_summary,
        error_summary=run.error_summary,
        log_tail=run.log_tail,
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _get_completed_run(session: Session, run_id: UUID) -> ExperimentRun:
    run = get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Experimento não encontrado")
    if run.status != "succeeded" or not run.result_path:
        raise HTTPException(status_code=409, detail="Resultado do experimento ainda não está pronto")
    try:
        result_file_for_run(run)
    except FileNotFoundError as error:
        raise HTTPException(status_code=410, detail="Arquivo do resultado não está disponível") from error
    return run


def _data_manifest_fingerprint() -> str:
    matrix_manifest_path = PROJECT_ROOT / "data/manifests/travel-matrix-mt.json"
    graph_manifest_path = PROJECT_ROOT / "data/manifests/road-network-mt.json"
    matrix = json.loads(matrix_manifest_path.read_text(encoding="utf-8"))
    graph = json.loads(graph_manifest_path.read_text(encoding="utf-8"))
    components = f"{matrix['cache_sha256']}:{graph['graph_sha256']}"
    return hashlib.sha256(components.encode("ascii")).hexdigest()
