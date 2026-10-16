from __future__ import annotations

import gzip
import json
import sqlite3
import zlib
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.jobs.worker import LocalExperimentWorker
from app.core import memory_limit
from app.api.routes import jobs as jobs_routes
from app.api.routes import resources as resources_routes
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


def test_memory_budget_api_uses_eighty_percent_of_available_memory(monkeypatch) -> None:
    factory = _session_factory()
    app = create_app(session_factory=factory, job_worker=FakeWorker(factory), start_worker=False)
    monkeypatch.setattr(resources_routes, "available_memory_bytes", lambda: 3_000_000_000)
    monkeypatch.setattr(memory_limit, "_current_cgroup_remaining_bytes", lambda: None)

    with TestClient(app) as client:
        budget = client.get("/api/resources/memory-budget")

    assert budget.status_code == 200
    assert budget.json() == {
        "available_memory_bytes": 3_000_000_000,
        "budget_bytes": 2_400_000_000,
        "hard_limit_bytes": 25_000_000_000,
        "fraction_of_available": 0.8,
        "minimum_budget_bytes": 1_000_000_000,
        "can_start": True,
    }


def test_experiment_report_is_cached_and_invalidated_after_file_change(tmp_path, monkeypatch) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text('{"revision":1}', encoding="utf-8")
    monkeypatch.setattr(experiment_results, "result_file_for_run", lambda _run: report_path)

    first = experiment_results._read_report(object())
    assert experiment_results._read_report(object()) is first

    report_path.write_text('{"revision":22}', encoding="utf-8")
    assert experiment_results._read_report(object()) == {"revision": 22}


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


def test_job_request_rejects_when_memory_budget_is_below_minimum(monkeypatch) -> None:
    factory = _session_factory()
    worker = FakeWorker(factory)
    app = create_app(session_factory=factory, job_worker=worker, start_worker=False)
    monkeypatch.setattr(jobs_routes, "memory_budget_bytes", lambda: 999_000_000)

    with TestClient(app) as client:
        blocked = client.post("/api/jobs", json={"job_type": "mtsp"})
        jobs = client.get("/api/jobs")

    assert blocked.status_code == 409
    assert "Memória livre insuficiente" in blocked.json()["detail"]
    assert jobs.json() == []
    assert worker.enqueued == []


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
    (root / "data/processed/municipalities-mt.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [-56.0, -15.0]},
                        "properties": {"ibge_code": 1},
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [-55.5, -15.0]},
                        "properties": {"ibge_code": 2},
                    },
                ],
            }
        ),
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
    with sqlite3.connect(report_path.with_suffix(".paths.sqlite")) as path_store:
        path_store.execute(
            "CREATE TABLE candidate_route_paths ("
            "candidate_id TEXT PRIMARY KEY, edge_ids BLOB NOT NULL)"
        )
        path_store.execute(
            "INSERT INTO candidate_route_paths (candidate_id, edge_ids) VALUES (?, ?)",
            ("candidate-1", zlib.compress(json.dumps(["road-1"]).encode("utf-8"))),
        )
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
    preview_path = root / "data/processed/jobs" / f"{run_id}.preview.json"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_text(
        json.dumps({"routes": [{"agent_id": 1, "visit_order": [1, 2]}]}),
        encoding="utf-8",
    )
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
        preview = client.get(f"/api/jobs/{run_id}/preview")
        preview_path.unlink()
        pending_preview = client.get(f"/api/jobs/{run_id}/preview")

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
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store"
    assert preview.json()["features"][0]["geometry"]["coordinates"] == [
        [-56.0, -15.0],
        [-55.5, -15.0],
    ]
    assert pending_preview.status_code == 200
    assert pending_preview.json() == {"type": "FeatureCollection", "features": []}


def test_open_tsp_geojson_uses_road_edges_from_matrix_paths(
    tmp_path: Path, monkeypatch
) -> None:
    factory = _session_factory()
    root = tmp_path
    for folder in ("data/processed", "data/matrices", "data/results", "data/manifests"):
        (root / folder).mkdir(parents=True)
    graph_records = [
        {"type": "node", "osm_id": 1, "longitude": -56.0, "latitude": -15.0},
        {"type": "node", "osm_id": 2, "longitude": -55.5, "latitude": -15.0},
        {"type": "edge", "edge_id": "road-1", "source": 1, "target": 2},
    ]
    with gzip.open(root / "data/processed/road-network.jsonl.gz", "wt", encoding="utf-8") as output:
        for record in graph_records:
            output.write(json.dumps(record, separators=(",", ":")) + "\n")
    with gzip.open(root / "data/matrices/matrix.json.gz", "wt", encoding="utf-8") as output:
        json.dump(
            {
                "municipalities": [{"ibge_code": 1}, {"ibge_code": 2}],
                "distance": {"paths": [[[], ["road-1"]], [["road-1"], []]]},
            },
            output,
        )
    (root / "data/manifests/road-network-mt.json").write_text(
        json.dumps({"graph_path": "data/processed/road-network.jsonl.gz"}), encoding="utf-8"
    )
    (root / "data/manifests/travel-matrix-mt.json").write_text(
        json.dumps({"cache_path": "data/matrices/matrix.json.gz", "cache_sha256": "matrixhash"}),
        encoding="utf-8",
    )
    scenario = "O1-fixed-cuiaba-distance"
    report_path = root / "data/results/d4.json"
    report_path.write_text(
        json.dumps(
            {
                "fingerprint": {"matrix_cache_sha256": "matrixhash"},
                "scenarios": [
                    {
                        "scenario": scenario,
                        "status": "feasible",
                        "objective": "distance",
                        "visit_order": [1, 2],
                        "legs": [
                            {"from_ibge_code": 1, "to_ibge_code": 2, "edge_ids": ["road-1"]}
                        ],
                        "distance_total_m": 420.0,
                        "completion_time_s": 30.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jobs_routes, "PROJECT_ROOT", root)
    monkeypatch.setattr(experiment_results, "PROJECT_ROOT", root)
    monkeypatch.setattr(jobs_routes, "result_file_for_run", lambda _run: report_path)
    monkeypatch.setattr(experiment_results, "result_file_for_run", lambda _run: report_path)
    with factory() as session:
        run = ExperimentRun(
            job_type="tsp",
            status="succeeded",
            parameters={"job_type": "tsp"},
            result_path="data/results/d4.json",
            algorithm_version="tsp-v1",
        )
        session.add(run)
        session.commit()
        run_id = run.id
    app = create_app(session_factory=factory, job_worker=FakeWorker(factory), start_worker=False)

    def fail_if_matrix_reload(*_args, **_kwargs):
        raise AssertionError("cache hit should not load matrix paths")

    with TestClient(app) as client:
        geojson = client.get(
            f"/api/experiments/{run_id}/geojson", params={"scenario": scenario, "policy": "P1"}
        )
        monkeypatch.setattr(jobs_routes, "result_routes", fail_if_matrix_reload)
        cached_geojson = client.get(
            f"/api/experiments/{run_id}/geojson", params={"scenario": scenario, "policy": "P1"}
        )

    assert geojson.status_code == 200
    assert cached_geojson.status_code == 200
    features = geojson.json()["features"]
    assert len(features) == 1
    assert features[0]["geometry"] == {
        "type": "LineString",
        "coordinates": [[-56.0, -15.0], [-55.5, -15.0]],
    }
    assert features[0]["properties"]["agent_id"] == 1


def test_legacy_tsp_route_uses_approximate_seat_geometry_without_matrix(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path
    report_path = root / "data/results/legacy.json"
    report_path.parent.mkdir(parents=True)
    seats_path = root / "data/processed/municipalities-mt.geojson"
    seats_path.parent.mkdir(parents=True)
    seats_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [-56.0, -15.0]},
                        "properties": {"ibge_code": 1},
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [-55.5, -15.0]},
                        "properties": {"ibge_code": 2},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(
            {
                "scenarios": [
                    {
                        "scenario": "legacy",
                        "status": "feasible",
                        "visit_order": [1, 2],
                        "distance_total_m": 42.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        experiment_results, "result_file_for_run", lambda _run: report_path
    )
    run = ExperimentRun(
        id=uuid4(),
        job_type="tsp",
        status="succeeded",
        parameters={"job_type": "tsp"},
        result_path="data/results/legacy.json",
        algorithm_version="tsp-v1",
    )

    routes = experiment_results.result_routes(run, scenario="legacy")
    geojson_path = experiment_results.build_geojson_file(
        run, routes, cache_key="legacy", project_root=root
    )
    feature = json.loads(geojson_path.read_text(encoding="utf-8"))["features"][0]

    assert routes[0]["geometry_approximate"] is True
    assert feature["geometry"]["coordinates"] == [[-56.0, -15.0], [-55.5, -15.0]]
    assert feature["properties"]["geometry_approximate"] is True


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
            "objective": "time",
            "vehicle_count": 3,
            "origin_ibge_code": 5100102,
        },
        Path("/tmp/overlap-report.json"),
    )
    worker.shutdown()

    assert command[1].endswith("experiments/runners/d6_custom.py")
    assert "--workers" in command
    assert "--variants" in command
    assert "--vehicle-count" in command
    assert "--origin-ibge-code" in command
    assert "time" in command
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
