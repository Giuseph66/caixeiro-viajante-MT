from __future__ import annotations

import gzip
import json
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.jobs.worker import LocalExperimentWorker
from app.api.routes import jobs as jobs_routes
from app.main import create_app
from app.persistence.base import Base
from app.persistence.models.experiment_run import ExperimentRun
from app.services import experiment_results


class FakeWorker:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.enqueued: list[UUID] = []

    def enqueue(self, run_id: UUID) -> None:
        self.enqueued.append(run_id)

    def cancel(self, run_id: UUID) -> str | None:
        with self.session_factory() as session:
            run = session.get(ExperimentRun, run_id)
            if run is None:
                return None
            run.status = "interrupted"
            session.commit()
            return run.status


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_api_exposes_official_municipalities_datasets_and_geojson() -> None:
    factory = _session_factory()
    app = create_app(session_factory=factory, job_worker=FakeWorker(factory), start_worker=False)

    with TestClient(app) as client:
        municipalities = client.get("/api/municipalities")
        datasets = client.get("/api/datasets")
        geojson = client.get("/api/geojson/municipalities")
        configuration = client.get("/api/configurations")

    assert municipalities.status_code == 200
    assert len(municipalities.json()) == 142
    assert municipalities.json()[0]["ibge_code"] == 5100102
    assert len(datasets.json()) == 6
    assert geojson.status_code == 200
    assert geojson.headers["content-type"].startswith("application/geo+json")
    assert len(geojson.json()["features"]) == 142
    assert configuration.json()["limits"]["memory_limit_bytes"] == 25_000_000_000


def test_job_api_persists_queued_job_and_reports_metrics() -> None:
    factory = _session_factory()
    worker = FakeWorker(factory)
    app = create_app(session_factory=factory, job_worker=worker, start_worker=False)

    with TestClient(app) as client:
        created = client.post("/api/experiments", json={"job_type": "overlap", "variants": 2})
        run_id = created.json()["id"]
        fetched = client.get(f"/api/jobs/{run_id}")
        listed = client.get("/api/jobs")
        metrics = client.get("/api/metrics")
        cancelled = client.post(f"/api/jobs/{run_id}/cancel")

    assert created.status_code == 202
    assert created.json()["status"] == "queued"
    assert created.json()["progress_percent"] == 0
    assert worker.enqueued == [UUID(run_id)]
    assert fetched.json()["id"] == run_id
    assert listed.json()[0]["id"] == run_id
    assert metrics.json()["queued"] == 1
    assert cancelled.json()["status"] == "interrupted"


def test_job_request_rejects_unsafe_worker_or_lambda_values() -> None:
    factory = _session_factory()
    app = create_app(session_factory=factory, job_worker=FakeWorker(factory), start_worker=False)

    with TestClient(app) as client:
        too_many_workers = client.post(
            "/api/jobs", json={"job_type": "mtsp", "workers": 9}
        )
        missing_zero_lambda = client.post(
            "/api/jobs", json={"job_type": "overlap", "lambdas": [1.0]}
        )

    assert too_many_workers.status_code == 422
    assert missing_zero_lambda.status_code == 422


def test_experiment_results_api_exports_metrics_routes_and_road_geojson(
    tmp_path: Path, monkeypatch
) -> None:
    factory = _session_factory()
    root = tmp_path
    graph_path = root / "data/processed/road-network.jsonl.gz"
    matrix_path = root / "data/matrices/matrix.json.gz"
    report_path = root / "data/results/report.json"
    graph_path.parent.mkdir(parents=True)
    matrix_path.parent.mkdir(parents=True)
    report_path.parent.mkdir(parents=True)
    (root / "data/manifests").mkdir(parents=True)

    graph_records = [
        {"type": "header"},
        {"type": "node", "osm_id": 1, "longitude": -56.0, "latitude": -15.0},
        {"type": "node", "osm_id": 2, "longitude": -55.5, "latitude": -15.0},
        {
            "type": "edge",
            "edge_id": "road-1",
            "source": 1,
            "target": 2,
        },
    ]
    with gzip.open(graph_path, "wt", encoding="utf-8") as output:
        for record in graph_records:
            output.write(json.dumps(record, separators=(",", ":")) + "\n")
    matrix_payload = {
        "municipalities": [{"ibge_code": 1}, {"ibge_code": 2}],
        "distance": {"paths": [[[], ["road-1"]], [["road-1"], []]]},
    }
    with gzip.open(matrix_path, "wt", encoding="utf-8") as output:
        json.dump(matrix_payload, output)
    (root / "data/manifests/road-network-mt.json").write_text(
        json.dumps({"graph_path": "data/processed/road-network.jsonl.gz"}), encoding="utf-8"
    )
    (root / "data/manifests/travel-matrix-mt.json").write_text(
        json.dumps({"cache_path": "data/matrices/matrix.json.gz", "cache_sha256": "matrixhash"}),
        encoding="utf-8",
    )
    scenario = "O1-fixed-cuiaba-k2-distance"
    report = {
        "fingerprint": {"matrix_cache_sha256": "matrixhash"},
        "scenarios": [
            {
                "scenario": scenario,
                "objective": "distance",
                "vehicle_count": 2,
                "candidate_solution_count": 1,
                "best_by_policy": {
                    "P2": [
                        {
                            "lambda": 1.0,
                            "status": "feasible",
                            "origin_ibge_code": 1,
                            "candidate_ids": ["candidate-1"],
                        }
                    ]
                },
                "origins": [
                    {
                        "origin_ibge_code": 1,
                        "p2": [
                            {
                                "lambda": 1.0,
                                "routes": [
                                    {
                                        "candidate_id": "candidate-1",
                                        "agent_id": 1,
                                        "visit_order": [1, 2],
                                        "distance_m": 420.0,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(jobs_routes, "PROJECT_ROOT", root)
    monkeypatch.setattr(experiment_results, "PROJECT_ROOT", root)
    monkeypatch.setattr(jobs_routes, "result_file_for_run", lambda _run: report_path)
    monkeypatch.setattr(experiment_results, "result_file_for_run", lambda _run: report_path)

    with factory() as session:
        run = ExperimentRun(
            job_type="overlap",
            status="succeeded",
            parameters={"job_type": "overlap"},
            result_path="data/results/report.json",
            algorithm_version="overlap-v1",
        )
        session.add(run)
        session.commit()
        run_id = run.id
    app = create_app(session_factory=factory, job_worker=FakeWorker(factory), start_worker=False)

    with TestClient(app) as client:
        metrics = client.get(f"/api/experiments/{run_id}/metrics")
        routes = client.get(
            f"/api/experiments/{run_id}/routes",
            params={"scenario": scenario, "policy": "P2", "lambda": 1.0},
        )
        geojson = client.get(
            f"/api/experiments/{run_id}/geojson",
            params={"scenario": scenario, "policy": "P2", "lambda": 1.0},
        )
        exported = client.get(f"/api/experiments/{run_id}/export")
        datasets = client.get("/api/datasets/status")
        algorithms = client.get("/api/algorithms")
        configurations = client.get("/api/experiments/configurations")

    assert metrics.status_code == 200
    assert metrics.json()["scenarios"][0]["best_by_policy"]["P2"][0]["status"] == "feasible"
    assert metrics.json()["scenarios"][0]["metrics_by_policy"]["P2"][0]["total_distance_m"] == 420
    assert routes.status_code == 200
    assert routes.json()["routes"][0]["legs"][0]["edge_ids"] == ["road-1"]
    assert geojson.status_code == 200
    assert geojson.headers["content-type"].startswith("application/geo+json")
    assert geojson.json()["features"][0]["geometry"]["coordinates"] == [
        [-56.0, -15.0],
        [-55.5, -15.0],
    ]
    assert exported.status_code == 200
    assert exported.json()["scenarios"][0]["scenario"] == scenario
    assert len(datasets.json()) == 6
    assert {item["id"] for item in algorithms.json()} == {"tsp", "mtsp", "overlap"}
    assert configurations.status_code == 200


def test_local_worker_maps_job_type_and_progress_output() -> None:
    worker = LocalExperimentWorker(_session_factory(), Path.cwd())
    command = worker._build_command(
        "overlap",
        {
            "time_limit_s": 60,
            "workers": 8,
            "seed": 20261008,
            "variants": 4,
            "lambdas": [0.0, 1.0],
        },
        Path("/tmp/overlap-report.json"),
    )
    worker.shutdown()

    assert command[1].endswith("experiments/runners/d6_overlap.py")
    assert "--workers" in command
    assert "--variants" in command
    assert LocalExperimentWorker._progress_from_line("[4/8] cenário", 10) == 50
    assert LocalExperimentWorker._progress_from_line("auditoria", 50) == 50


def test_worker_marks_orphaned_running_jobs_interrupted_on_restart() -> None:
    factory = _session_factory()
    with factory() as session:
        session.add(
            ExperimentRun(
                job_type="mtsp",
                status="running",
                parameters={"job_type": "mtsp"},
                progress_percent=35,
                progress_stage="Calculando",
            )
        )
        session.commit()
        run_id = session.query(ExperimentRun).one().id

    worker = LocalExperimentWorker(factory, Path.cwd())
    worker.start()
    worker.shutdown()
    with factory() as session:
        run = session.get(ExperimentRun, run_id)

    assert run is not None
    assert run.status == "interrupted"
    assert run.progress_percent == 35
    assert "reinício" in (run.progress_stage or "")
