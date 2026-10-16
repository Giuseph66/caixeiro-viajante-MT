from __future__ import annotations

import importlib.util
import gzip
import hashlib
import json
import sys
from concurrent.futures import Future
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.jobs.worker import LocalExperimentWorker
from app.optimization.overlap import (
    PhysicalEdgeMetric,
    RouteCandidate,
    assess_policy,
    solve_route_pool,
)
from app.optimization.repetition import refine_route_repetition, repeated_distance
from app.routing.matrices import MetricMatrix, SeatSnap
from app.schemas.api import JobCreateRequest
from app.services import experiment_results

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "route_repetition_runner", ROOT / "experiments/runners/route_repetition.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def problem(metric="distance"):
    # A -> B -> A -> C costs 30 m but repeats AB; A -> C -> B costs 31 m, no repetition.
    edges = {
        "AB": PhysicalEdgeMetric("AB", "ab", 10, 0, 1, 1),
        "BA": PhysicalEdgeMetric("BA", "ab", 10, 1, 0, 1),
        "AC": PhysicalEdgeMetric("AC", "ac", 10, 0, 2, 1),
        "CB": PhysicalEdgeMetric("CB", "cb", 21, 2, 1, 2.1),
    }
    paths = [[[], ["AB"], ["AC"]], [["BA"], [], ["BA", "AC"]], [["CB", "BA"], ["CB"], []]]
    costs = [
        [
            sum(
                getattr(edges[e], "length_m" if metric == "distance" else "travel_time_s")
                for e in path
            )
            for path in row
        ]
        for row in paths
    ]
    seats = [SeatSnap(code, code, 0, 0, i, 0, True) for i, code in enumerate((1, 2, 3))]
    return seats, MetricMatrix(metric, "m" if metric == "distance" else "s", costs, paths), edges


def test_counts_reverse_direction_and_each_additional_traversal():
    _, _, edges = problem()
    assert repeated_distance(["AB", "BA", "AB"], edges) == 20
    a = RouteCandidate("a", 1, (2,), ("AB",), 10, 1)
    b = RouteCandidate("b", 2, (3,), ("BA",), 10, 1)
    assessment = assess_policy(
        [a, b], edges, objective="distance", policy="P1", repetition_policy="forbid"
    )
    assert assessment.feasible
    assert assessment.repeated_distance_m == 0
    assert assessment.overlap.redundant_distance_m == 10
    assert not assess_policy([a, b], edges, objective="distance", policy="P3").feasible
    repeated = RouteCandidate("repeated", 1, (2, 3), ("AB", "BA", "AC"), 30, 3)
    combined = assess_policy(
        [repeated, b], edges, objective="distance", policy="P2", repetition_policy="reduce"
    )
    assert combined.base_objective == 40
    assert combined.penalty_value == combined.repetition_penalty_value == 10
    assert combined.objective_value == 60


@pytest.mark.parametrize("metric", ["distance", "time"])
def test_reduce_selects_longer_road_route_with_lower_penalized_cost(metric):
    seats, matrix, edges = problem(metric)
    services = {2: 5, 3: 7} if metric == "time" else {}
    allow = refine_route_repetition(
        [[1, 2, 3]],
        [s.ibge_code for s in seats],
        matrix,
        edges,
        policy="allow",
        time_limit_s=0.2,
        service_times_s=services,
    )
    reduce = refine_route_repetition(
        [[1, 2, 3]],
        [s.ibge_code for s in seats],
        matrix,
        edges,
        policy="reduce",
        time_limit_s=0.2,
        service_times_s=services,
    )
    assert allow.orders == ((1, 2, 3),)
    assert allow.repeated_distance_by_agent_m == (10,)
    assert reduce.orders == ((1, 3, 2),)
    assert reduce.repeated_distance_by_agent_m == (0,)
    assert reduce.objective_value == pytest.approx(31 if metric == "distance" else 15.1)


def test_forbid_respects_fixed_destination_and_returns_no_violating_route(tmp_path, monkeypatch):
    seats, matrix, edges = problem()
    scenario = {
        "scenario": "fixed-to-3",
        "status": "feasible",
        "objective": "distance",
        "origin_policy": "fixed",
        "origin_ibge_code": 1,
        "destination_ibge_code": 3,
        "visit_order": [1, 2, 3],
        "objective_value": 30,
        "legs": [
            {"from_ibge_code": 1, "to_ibge_code": 2, "edge_ids": ["AB"]},
            {"from_ibge_code": 2, "to_ibge_code": 3, "edge_ids": ["BA", "AC"]},
        ],
    }
    preview = tmp_path / "preview.json"
    runner.apply_repetition_rule(
        scenario,
        seats,
        matrix,
        edges,
        policy="forbid",
        time_limit_s=0.2,
        seed=1,
        preview_path=preview,
    )
    assert scenario["status"] == "not_found"
    assert scenario["visit_order"] == scenario["legs"] == []
    assert scenario["objective_value"] is None
    assert scenario["repetition_search"]["best_repeated_distance_m"] == 10
    assert json.loads(preview.read_text())["routes"] == []
    monkeypatch.setattr(experiment_results, "_read_report", lambda _: {"scenarios": [scenario]})
    run = SimpleNamespace(
        id="r", job_type="tsp", algorithm_version="v", seed=1, data_manifest_sha256="x"
    )
    assert experiment_results._stored_routes(run, scenario="fixed-to-3") == []
    metrics = experiment_results.result_metrics(run)["scenarios"][0]
    assert metrics["repetition_policy"] == "forbid"
    assert metrics["repeated_distance_m"] is None


def test_runner_rebuilds_physical_legs_and_metrics_after_reordering():
    seats, matrix, edges = problem()
    scenario = {
        "scenario": "s",
        "status": "feasible",
        "objective": "distance",
        "origin_policy": "fixed",
        "origin_ibge_code": 1,
        "visit_order": [1, 2, 3],
        "objective_value": 30,
        "legs": [
            {"from_ibge_code": 1, "to_ibge_code": 2, "edge_ids": ["AB"]},
            {"from_ibge_code": 2, "to_ibge_code": 3, "edge_ids": ["BA", "AC"]},
        ],
    }
    runner.apply_repetition_rule(
        scenario, seats, matrix, edges, policy="reduce", time_limit_s=0.2, seed=1
    )
    assert scenario["visit_order"] == [1, 3, 2]
    assert [leg["edge_ids"] for leg in scenario["legs"]] == [["AC"], ["CB"]]
    assert scenario["repeated_distance_m"] == 0
    assert scenario["distance_total_m"] == scenario["base_objective_value"] == 31
    assert scenario["time_max_s"] == pytest.approx(3.1)


@pytest.mark.parametrize("repetition_policy", ["reduce", "forbid"])
def test_route_pool_applies_repetition_rule_independently_of_sharing(repetition_policy):
    _, _, edges = problem()
    bad = RouteCandidate("bad", 1, (2, 3), ("AB", "BA", "AC"), 30, 3)
    good = RouteCandidate("good", 1, (2, 3), ("AC", "CB"), 31, 3.1)
    other = RouteCandidate("other", 2, (4,), ("AB",), 10, 1)
    result = solve_route_pool(
        [bad, good, other],
        required_city_codes=[2, 3, 4],
        agent_ids=[1, 2],
        edge_metrics=edges,
        objective="distance",
        policy="P1",
        repetition_policy=repetition_policy,
    )
    assert result.status == "feasible"
    assert result.routes[0].candidate_id == "good"
    assert result.assessment.repeated_distance_m == 0
    assert result.assessment.base_objective == 41


@pytest.mark.parametrize("job_type,count", [("tsp", 1), ("mtsp", 2), ("overlap", 3)])
def test_request_persists_and_worker_forwards_repetition_policy(tmp_path, job_type, count):
    request = JobCreateRequest(job_type=job_type, vehicle_count=count, repetition_policy="forbid")
    worker = LocalExperimentWorker(lambda: None, ROOT)
    try:
        command = worker._build_command(job_type, request.model_dump(), tmp_path / "report.json")
        assert command[command.index("--repetition-policy") + 1] == "forbid"
    finally:
        worker.shutdown()
    assert JobCreateRequest(job_type=job_type, vehicle_count=count).repetition_policy == "allow"
    with pytest.raises(ValidationError):
        JobCreateRequest(job_type=job_type, vehicle_count=count, repetition_policy="unknown")


@pytest.fixture
def small_runner_environment(tmp_path, monkeypatch):
    # Exercise real runner report generation with a four-city star, no large datasets/processes.
    monkeypatch.setitem(
        sys.modules, "memory_limit", SimpleNamespace(enforce_memory_limit=lambda: 1_000_000_000)
    )
    monkeypatch.setitem(sys.modules, "route_repetition", runner)
    seats = [SeatSnap(i + 1, f"City {i}", 0, 0, i, 0, True) for i in range(4)]
    edges = {}
    for i in range(1, 4):
        for a, b in ((0, i), (i, 0)):
            edges[f"{a}-{b}"] = {
                "type": "edge",
                "edge_id": f"{a}-{b}",
                "physical_segment_id": f"road-{i}",
                "source": a,
                "target": b,
                "length_m": 10,
                "travel_time_s": 1,
            }
    paths = [
        [
            [] if a == b else ([f"{a}-{b}"] if a == 0 or b == 0 else [f"{a}-0", f"0-{b}"])
            for b in range(4)
        ]
        for a in range(4)
    ]
    payload = {"cache_key": "small", "municipalities": [asdict(seat) for seat in seats]}
    for metric in ("distance", "time"):
        payload[metric] = {
            "unit": "m" if metric == "distance" else "s",
            "costs": [
                [len(path) * (10 if metric == "distance" else 1) for path in row] for row in paths
            ],
            "paths": paths,
        }

    def write_gzip(name, data):
        with gzip.open(tmp_path / name, "wt") as stream:
            stream.write(data)
        return hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()

    matrix_sha = write_gzip("matrix.gz", json.dumps(payload))
    graph_sha = write_gzip(
        "graph.gz", "\n".join(json.dumps(edge, separators=(",", ":")) for edge in edges.values())
    )
    matrix_manifest = tmp_path / "matrix.json"
    matrix_manifest.write_text(
        json.dumps(
            {
                "cache_path": "matrix.gz",
                "cache_sha256": matrix_sha,
                "cache_key": "small",
                "graph_sha256": graph_sha,
                "municipality_count": 4,
            }
        )
    )
    graph_manifest = tmp_path / "graph.json"
    graph_manifest.write_text(json.dumps({"graph_path": "graph.gz", "graph_sha256": graph_sha}))

    class InlinePool:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def submit(self, function, *args):
            future = Future()
            try:
                future.set_result(function(*args))
            except Exception as error:
                future.set_exception(error)
            return future

    def load(name):
        module_spec = importlib.util.spec_from_file_location(
            name, ROOT / f"experiments/runners/{name}.py"
        )
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        monkeypatch.setattr(module, "ROOT", tmp_path)
        if hasattr(module, "CUIABA_IBGE_CODE"):
            monkeypatch.setattr(module, "CUIABA_IBGE_CODE", 1)
        if hasattr(module, "EXPECTED_MUNICIPALITIES"):
            monkeypatch.setattr(module, "EXPECTED_MUNICIPALITIES", 4)
        if hasattr(module, "_sha256"):
            original_hash = module._sha256
            monkeypatch.setattr(
                module,
                "_sha256",
                lambda path: original_hash(path) if path.is_file() else "test-code",
            )
        if hasattr(module, "ProcessPoolExecutor"):
            monkeypatch.setattr(module, "ProcessPoolExecutor", InlinePool)
            monkeypatch.setattr(module, "_memory_current_bytes", lambda: 0)
        return module

    return load, matrix_manifest, graph_manifest


def test_custom_overlap_forwards_rule_and_reserves_base_time(
    small_runner_environment, tmp_path, monkeypatch
):
    load, _, _ = small_runner_environment
    custom = load("d6_custom")
    commands = []
    monkeypatch.setattr(
        custom.subprocess, "run", lambda command, **_kwargs: commands.append(command)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "custom",
            "--time-limit-s",
            "100",
            "--variants",
            "2",
            "--workers",
            "1",
            "--seed",
            "1",
            "--vehicle-count",
            "2",
            "--objective",
            "distance",
            "--origin-mode",
            "fixed",
            "--origin-ibge-code",
            "1",
            "--preview-path",
            str(tmp_path / "preview.json"),
            "--output",
            str(tmp_path / "result.json"),
            "--lambdas",
            "0",
            "1",
            "--repetition-policy",
            "forbid",
        ],
    )
    custom._main()
    base, overlap = commands
    assert float(base[base.index("--time-limit-s") + 1]) == 75
    assert float(overlap[overlap.index("--time-limit-s") + 1]) == 100
    assert overlap[overlap.index("--repetition-policy") + 1] == "forbid"


def test_d4_and_d5_runners_do_not_publish_forbidden_repetitions(
    small_runner_environment, tmp_path, monkeypatch
):
    load, matrix_manifest, graph_manifest = small_runner_environment
    for name, matrix_flag, agents in (
        ("d4_open_tsp", "--cache-manifest", []),
        ("d5_open_mtsp", "--matrix-manifest", ["--vehicle-count", "2", "--workers", "1"]),
    ):
        module = load(name)
        output = tmp_path / f"{name}.json"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                name,
                matrix_flag,
                str(matrix_manifest),
                "--graph-manifest",
                str(graph_manifest),
                "--output",
                str(output),
                "--objective",
                "distance",
                "--origin-mode",
                "fixed",
                "--origin-ibge-code",
                "1",
                "--repetition-policy",
                "forbid",
                "--time-limit-s",
                "0.04",
                *agents,
            ],
        )
        module._main()
        scenario = json.loads(output.read_text())["scenarios"][0]
        assert scenario["status"] == "not_found"
        assert scenario.get("routes", scenario.get("visit_order")) == []
        assert scenario["objective_value"] is None


@pytest.mark.parametrize("repetition_policy", ["reduce", "forbid"])
def test_d5_and_d6_runners_reconstruct_penalized_and_physical_metrics(
    small_runner_environment, tmp_path, monkeypatch, repetition_policy
):
    load, matrix_manifest, graph_manifest = small_runner_environment
    output = tmp_path / "mtsp.json"
    module = load("d5_open_mtsp")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "d5",
            "--matrix-manifest",
            str(matrix_manifest),
            "--graph-manifest",
            str(graph_manifest),
            "--output",
            str(output),
            "--objective",
            "distance",
            "--origin-mode",
            "fixed",
            "--origin-ibge-code",
            "1",
            "--vehicle-count",
            "2",
            "--workers",
            "1",
            "--repetition-policy",
            "reduce",
            "--time-limit-s",
            "0.04",
        ],
    )
    module._main()
    scenario = json.loads(output.read_text())["scenarios"][0]
    assert scenario["status"] == "feasible"
    assert scenario["total_distance_m"] == scenario["base_objective_value"] == 40
    assert scenario["repeated_distance_m"] == scenario["repetition_penalty_value"] == 10
    assert scenario["objective_value"] == 50
    d6 = load("d6_overlap")
    overlap_output = tmp_path / "overlap.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "d6",
            "--matrix-manifest",
            str(matrix_manifest),
            "--graph-manifest",
            str(graph_manifest),
            "--mtsp-report",
            str(output),
            "--output",
            str(overlap_output),
            "--variants",
            "1",
            "--repetition-policy",
            repetition_policy,
            "--time-limit-s",
            "0.04",
        ],
    )
    d6._main()
    best = json.loads(overlap_output.read_text())["scenarios"][0]["best_by_policy"]["P1"]
    if repetition_policy == "forbid":
        assert best is None
        return
    assert best["repeated_distance_m"] == best["repetition_penalty_value"] == 10
    assert best["objective_value"] == 50
