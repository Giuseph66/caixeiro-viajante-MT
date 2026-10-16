from __future__ import annotations

import gzip
import json
from pathlib import Path
from uuid import uuid4

from app.persistence.models.experiment_run import ExperimentRun
from app.services import experiment_results, route_legs


def _write_project(root: Path, *, cost: float) -> Path:
    jobs = root / "data/processed/jobs"
    jobs.mkdir(parents=True)
    (root / "data/manifests").mkdir(parents=True)
    (root / "data/matrices").mkdir(parents=True)
    municipalities = [{"ibge_code": code} for code in (1, 2, 3)]
    paths = [
        [[], ["e12"], ["e12", "e23"]],
        [["e21"], [], ["e23"]],
        [["e32", "e21"], ["e32"], []],
    ]
    costs = [[0.0, 10.0, 25.0], [10.0, 0.0, 15.0], [25.0, 15.0, 0.0]]
    cache = {
        "municipalities": municipalities,
        "distance": {"costs": costs, "paths": paths},
        "time": {"costs": costs, "paths": paths},
    }
    with gzip.open(root / "data/matrices/m.json.gz", "wt", encoding="utf-8") as output:
        json.dump(cache, output)
    (root / "data/manifests/travel-matrix-mt.json").write_text(
        json.dumps({"cache_path": "data/matrices/m.json.gz", "cache_key": "key"}),
        encoding="utf-8",
    )
    result_path = jobs / "run.json"
    result_path.write_text(
        json.dumps(
            {
                "fingerprint": {"matrix_cache_key": "key"},
                "scenarios": [
                    {
                        "scenario": "old",
                        "status": "feasible",
                        "objective": "distance",
                        "visit_order": [1, 2, 3],
                        "travel_cost": cost,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return result_path


def test_backfill_legs_rebuilds_road_paths_from_matrix(tmp_path: Path) -> None:
    result_path = _write_project(tmp_path, cost=25.0)
    stale = result_path.with_name("run-abc.geojson")
    stale.write_text("{}", encoding="utf-8")

    assert route_legs.backfill_legs(tmp_path) == 1

    assert not stale.exists()
    sidecar = route_legs.load_legs_sidecar(result_path)
    assert sidecar[("old", (1, 2, 3))] == [{"edge_ids": ["e12"]}, {"edge_ids": ["e23"]}]
    routes = route_legs.attach_sidecar_legs(
        result_path,
        "old",
        [{"visit_order": [1, 2, 3], "geometry_approximate": True}],
    )
    assert routes[0]["legs"] == [{"edge_ids": ["e12"]}, {"edge_ids": ["e23"]}]
    assert "geometry_approximate" not in routes[0]
    assert route_legs.backfill_legs(tmp_path) == 0


def test_backfill_legs_skips_route_when_matrix_cost_diverges(tmp_path: Path) -> None:
    result_path = _write_project(tmp_path, cost=999.0)

    assert route_legs.backfill_legs(tmp_path) == 0
    assert not route_legs.legs_sidecar_path(result_path).exists()


def test_result_routes_keeps_approximation_without_sidecar(tmp_path: Path, monkeypatch) -> None:
    result_path = _write_project(tmp_path, cost=25.0)
    monkeypatch.setattr(experiment_results, "result_file_for_run", lambda _run: result_path)
    run = ExperimentRun(
        id=uuid4(),
        job_type="tsp",
        status="succeeded",
        parameters={},
        result_path="data/processed/jobs/run.json",
        algorithm_version="tsp-v1",
    )

    routes = experiment_results.result_routes(run, scenario="old")

    assert routes[0]["geometry_approximate"] is True
