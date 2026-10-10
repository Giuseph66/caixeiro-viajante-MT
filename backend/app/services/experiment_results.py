from __future__ import annotations

import gzip
import hashlib
import json
import math
import threading
from pathlib import Path
from typing import Any, Literal

from app.core.config import PROJECT_ROOT
from app.persistence.models.experiment_run import ExperimentRun

OverlapPolicy = Literal["P1", "P2", "P3"]
_GEOJSON_LOCK = threading.Lock()


def result_file_for_run(
    run: ExperimentRun, *, project_root: Path = PROJECT_ROOT
) -> Path:
    if not run.result_path:
        raise FileNotFoundError("Resultado não registrado")
    root = project_root.resolve()
    result_path = (root / run.result_path).resolve()
    if not result_path.is_relative_to(root) or not result_path.is_file():
        raise FileNotFoundError("Arquivo de resultado ausente")
    return result_path


def result_metrics(run: ExperimentRun) -> dict[str, Any]:
    report = _read_report(run)
    scenarios = report.get("scenarios", [])
    summaries = []
    for scenario in scenarios:
        if "origins" in scenario:
            best_by_policy = scenario.get("best_by_policy", {})
            summaries.append(
                {
                    "scenario": scenario.get("scenario"),
                    "objective": scenario.get("objective"),
                    "vehicle_count": scenario.get("vehicle_count"),
                    "candidate_solution_count": scenario.get("candidate_solution_count"),
                    "best_by_policy": best_by_policy,
                    "metrics_by_policy": _overlap_metrics_by_policy(scenario, best_by_policy),
                }
            )
            continue
        summaries.append(
            {
                "scenario": scenario.get("scenario"),
                "status": scenario.get("status"),
                "objective": scenario.get("objective"),
                "unit": scenario.get("unit"),
                "origin_policy": scenario.get("origin_policy"),
                "origin_ibge_code": scenario.get("origin_ibge_code"),
                "vehicle_count": scenario.get("vehicle_count", len(scenario.get("routes", []))),
                "objective_value": scenario.get("objective_value"),
                "total_distance_m": scenario.get("total_distance_m"),
                "time_max_s": scenario.get("time_max_s"),
                "runtime_s": scenario.get("runtime_s"),
                "routes": [
                    {
                        "agent_id": route.get("agent_id"),
                        "municipality_count": max(0, len(route.get("visit_order", [])) - 1),
                        "distance_m": route.get("distance_m"),
                        "road_time_s": route.get("road_time_s"),
                        "completion_time_s": route.get("completion_time_s"),
                    }
                    for route in scenario.get("routes", [])
                ],
            }
        )
    return {
        "run_id": str(run.id),
        "job_type": run.job_type,
        "algorithm_version": run.algorithm_version,
        "seed": run.seed,
        "data_manifest_sha256": run.data_manifest_sha256,
        "fingerprint": report.get("fingerprint"),
        "configuration": report.get("configuration"),
        "scenario_count": len(summaries),
        "scenarios": summaries,
    }


def _overlap_metrics_by_policy(
    scenario: dict[str, Any], best_by_policy: dict[str, Any]
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for policy in ("P1", "P2", "P3"):
        best = best_by_policy.get(policy)
        if isinstance(best, list):
            output[policy] = [
                _augment_overlap_metrics(scenario, policy, item)
                for item in best
                if isinstance(item, dict)
            ]
        else:
            output[policy] = _augment_overlap_metrics(scenario, policy, best)
    return output


def _augment_overlap_metrics(
    scenario: dict[str, Any], policy: str, best: dict[str, Any] | None
) -> dict[str, Any] | None:
    if not best or not best.get("candidate_ids"):
        return best
    origin = next(
        (item for item in scenario.get("origins", []) if item.get("origin_ibge_code") == best.get("origin_ibge_code")),
        None,
    )
    if origin is None:
        return best
    if policy == "P1":
        summary = origin.get("p1")
    elif policy == "P3":
        summary = origin.get("p3")
    else:
        summary = next(
            (item for item in origin.get("p2", []) if item.get("lambda") == best.get("lambda")),
            None,
        )
    if not isinstance(summary, dict):
        return best
    candidates = set(best["candidate_ids"])
    routes = [
        route for route in summary.get("routes", []) if route.get("candidate_id") in candidates
    ]
    distance_values = [route.get("distance_m") for route in routes]
    time_values = [route.get("completion_time_s") for route in routes]
    return {
        **best,
        "total_distance_m": sum(distance_values)
        if routes and all(isinstance(value, (int, float)) for value in distance_values)
        else None,
        "time_max_s": max(time_values)
        if routes and all(isinstance(value, (int, float)) for value in time_values)
        else None,
    }


def result_routes(
    run: ExperimentRun,
    *,
    scenario: str,
    policy: OverlapPolicy = "P1",
    origin_ibge_code: int | None = None,
    lambda_value: float = 1.0,
) -> list[dict[str, Any]]:
    report = _read_report(run)
    scenario_report = next(
        (item for item in report.get("scenarios", []) if item.get("scenario") == scenario),
        None,
    )
    if scenario_report is None:
        raise ValueError(f"Cenário não encontrado: {scenario}")
    if "origins" not in scenario_report:
        return scenario_report.get("routes", [])

    summary, best = _overlap_selection(
        scenario_report,
        policy=policy,
        origin_ibge_code=origin_ibge_code,
        lambda_value=lambda_value,
    )
    if summary is None:
        return []
    best_candidates = set((best or {}).get("candidate_ids", []))
    routes = [
        dict(route)
        for route in summary.get("routes", [])
        if origin_ibge_code is not None or not best_candidates or route.get("candidate_id") in best_candidates
    ]
    if not routes:
        return []
    paths = _load_matrix_paths(PROJECT_ROOT, report, scenario_report["objective"])
    municipality_indexes = paths["municipality_indexes"]
    matrix = paths["paths"]
    for route in routes:
        visit_order = route.get("visit_order", [])
        legs = []
        for from_code, to_code in zip(visit_order, visit_order[1:], strict=False):
            try:
                from_index = municipality_indexes[int(from_code)]
                to_index = municipality_indexes[int(to_code)]
                edge_ids = matrix[from_index][to_index]
            except (KeyError, IndexError, TypeError) as error:
                raise ValueError("Matriz não contém a rota selecionada") from error
            if edge_ids is None:
                raise ValueError("Rota selecionada possui um trecho sem caminho")
            legs.append(
                {
                    "from_ibge_code": int(from_code),
                    "to_ibge_code": int(to_code),
                    "edge_ids": edge_ids,
                }
            )
        route["legs"] = legs
        route["scenario"] = scenario
    return routes


def build_geojson_file(
    run: ExperimentRun,
    routes: list[dict[str, Any]],
    *,
    cache_key: str,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    root = project_root.resolve()
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:20]
    output_path = root / "data/processed/jobs" / f"{run.id}-{digest}.geojson"
    with _GEOJSON_LOCK:
        if output_path.is_file():
            return output_path
        graph_manifest = json.loads(
            (root / "data/manifests/road-network-mt.json").read_text(encoding="utf-8")
        )
        graph_path = (root / graph_manifest["graph_path"]).resolve()
        if not graph_path.is_relative_to(root) or not graph_path.is_file():
            raise FileNotFoundError("Grafo rodoviário não está disponível")
        payload = _route_geojson(routes, graph_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_suffix(".geojson.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(output_path)
    return output_path


def _read_report(run: ExperimentRun) -> dict[str, Any]:
    with result_file_for_run(run).open("r", encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, dict):
        raise ValueError("Relatório do experimento inválido")
    return payload


def _overlap_selection(
    scenario: dict[str, Any],
    *,
    policy: OverlapPolicy,
    origin_ibge_code: int | None,
    lambda_value: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if origin_ibge_code is None:
        candidates = scenario.get("best_by_policy", {}).get(policy)
        if policy == "P2" and isinstance(candidates, list):
            best = next(
                (item for item in candidates if math.isclose(item.get("lambda", -1), lambda_value)),
                None,
            )
        else:
            best = candidates if isinstance(candidates, dict) else None
        if best is None or not best.get("candidate_ids"):
            return None, best
        origin_ibge_code = best.get("origin_ibge_code")
    else:
        best = None

    origin_report = next(
        (item for item in scenario.get("origins", []) if item.get("origin_ibge_code") == origin_ibge_code),
        None,
    )
    if origin_report is None:
        raise ValueError(f"Origem IBGE não encontrada no cenário: {origin_ibge_code}")
    if policy == "P1":
        summary = origin_report.get("p1")
    elif policy == "P3":
        summary = origin_report.get("p3")
    else:
        summary = next(
            (
                item
                for item in origin_report.get("p2", [])
                if math.isclose(item.get("lambda", -1), lambda_value)
            ),
            None,
        )
    return summary, best


def _load_matrix_paths(
    project_root: Path, report: dict[str, Any], objective: str
) -> dict[str, Any]:
    manifest_path = project_root / "data/manifests/travel-matrix-mt.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_hash = report.get("fingerprint", {}).get("matrix_cache_sha256")
    if expected_hash and manifest.get("cache_sha256") != expected_hash:
        raise ValueError("Matriz atual diverge da matriz usada no experimento")
    matrix_path = (project_root / manifest["cache_path"]).resolve()
    if not matrix_path.is_relative_to(project_root.resolve()) or not matrix_path.is_file():
        raise FileNotFoundError("Matriz de trajetórias não está disponível")
    with gzip.open(matrix_path, "rt", encoding="utf-8") as source:
        payload = json.load(source)
    if objective not in payload:
        raise ValueError(f"Objetivo não encontrado na matriz: {objective}")
    return {
        "municipality_indexes": {
            int(item["ibge_code"]): index for index, item in enumerate(payload["municipalities"])
        },
        "paths": payload[objective]["paths"],
    }


def _route_geojson(routes: list[dict[str, Any]], graph_path: Path) -> dict[str, Any]:
    wanted_edge_ids = {
        edge_id
        for route in routes
        for leg in route.get("legs", [])
        for edge_id in leg.get("edge_ids", [])
    }
    if not wanted_edge_ids:
        return {"type": "FeatureCollection", "features": []}

    node_coordinates: dict[int, tuple[float, float]] = {}
    edge_coordinates: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {}
    with gzip.open(graph_path, "rb") as source:
        for line in source:
            if b'"type":"node"' in line:
                node = json.loads(line)
                node_coordinates[int(node["osm_id"])] = (
                    float(node["longitude"]),
                    float(node["latitude"]),
                )
                continue
            if b'"type":"edge"' not in line:
                continue
            edge = json.loads(line)
            edge_id = edge["edge_id"]
            if edge_id not in wanted_edge_ids:
                continue
            try:
                edge_coordinates[edge_id] = (
                    node_coordinates[int(edge["source"])],
                    node_coordinates[int(edge["target"])],
                )
            except KeyError as error:
                raise ValueError(f"Nó ausente no grafo para a aresta {edge_id}") from error
            if len(edge_coordinates) == len(wanted_edge_ids):
                break
    missing = wanted_edge_ids - edge_coordinates.keys()
    if missing:
        raise ValueError(f"Arestas da rota ausentes no grafo: {len(missing)}")
    del node_coordinates

    features = []
    for route in routes:
        coordinates: list[tuple[float, float]] = []
        for leg in route.get("legs", []):
            for edge_id in leg.get("edge_ids", []):
                start, end = edge_coordinates[edge_id]
                if not coordinates:
                    coordinates.append(start)
                elif coordinates[-1] != start:
                    raise ValueError(f"Rota descontínua antes da aresta {edge_id}")
                coordinates.append(end)
        if len(coordinates) < 2:
            continue
        properties = {
            key: route[key]
            for key in (
                "scenario",
                "candidate_id",
                "agent_id",
                "visit_order",
                "distance_m",
                "road_time_s",
                "completion_time_s",
            )
            if key in route
        }
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "properties": properties,
            }
        )
    return {"type": "FeatureCollection", "features": features}
