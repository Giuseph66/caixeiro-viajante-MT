from __future__ import annotations

import gzip
import hashlib
import json
import math
import sqlite3
import threading
import zlib
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from app.core.config import PROJECT_ROOT
from app.persistence.models.experiment_run import ExperimentRun
from app.services.route_legs import attach_sidecar_legs

OverlapPolicy = Literal["P1", "P2", "P3"]
_GEOJSON_LOCK = threading.Lock()
_REPORT_CACHE_LOCK = threading.Lock()


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


def preview_geojson_for_run(
    run: ExperimentRun, *, project_root: Path = PROJECT_ROOT
) -> dict[str, Any]:
    root = project_root.resolve()
    preview_path = root / "data/processed/jobs" / f"{run.id}.preview.json"
    seats_path = root / "data/processed/municipalities-mt.geojson"
    if not preview_path.is_file() or not seats_path.is_file():
        raise FileNotFoundError("Rota candidata ainda não está disponível")
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    coordinates = _municipality_coordinates(seats_path)
    features = []
    for route in preview.get("routes", []):
        order = route.get("visit_order", [])
        if len(order) < 2 or any(int(code) not in coordinates for code in order):
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [coordinates[int(code)] for code in order],
                },
                "properties": {
                    "agent_id": int(route.get("agent_id", 1)),
                    "candidate_count": int(preview.get("candidate_count", 1)),
                    "objective_value": preview.get("objective_value"),
                    "scenario": preview.get("scenario"),
                },
            }
        )
    if not features:
        raise FileNotFoundError("Rota candidata ainda não está disponível")
    return {"type": "FeatureCollection", "features": features}


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
                    "repetition_policy": scenario.get("repetition_policy", "allow"),
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
                "total_distance_m": scenario.get("total_distance_m", scenario.get("distance_total_m")),
                "repetition_policy": scenario.get("repetition_policy", "allow"),
                "repeated_distance_m": scenario.get("repeated_distance_m"),
                "repetition_penalty_value": scenario.get("repetition_penalty_value"),
                "base_objective_value": scenario.get("base_objective_value"),
                "repetition_search": scenario.get("repetition_search"),
                "time_max_s": scenario.get("time_max_s"),
                "runtime_s": scenario.get("runtime_s"),
                "routes": [
                    {
                        "agent_id": route.get("agent_id"),
                        "municipality_count": max(0, len(route.get("visit_order", [])) - 1),
                        "distance_m": route.get("distance_m"),
                        "road_time_s": route.get("road_time_s"),
                        "completion_time_s": route.get("completion_time_s"),
                        "repeated_distance_m": route.get("repeated_distance_m"),
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
    routes = _stored_routes(
        run,
        scenario=scenario,
        policy=policy,
        origin_ibge_code=origin_ibge_code,
        lambda_value=lambda_value,
    )
    # Resultados antigos não gravaram `legs`; o arquivo auxiliar devolve as vias.
    return attach_sidecar_legs(result_file_for_run(run), scenario, routes)


def _stored_routes(
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
        if "routes" in scenario_report:
            return [dict(route) for route in scenario_report["routes"]]
        return _single_agent_routes(report, scenario_report, scenario)

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

    routes_without_geometry = [route for route in routes if not route.get("legs")]
    if routes_without_geometry:
        paths_path = result_file_for_run(run).with_suffix(".paths.sqlite")
        if paths_path.is_file():
            candidate_ids = [
                str(route["candidate_id"])
                for route in routes_without_geometry
                if route.get("candidate_id") is not None
            ]
            placeholders = ",".join("?" for _ in candidate_ids)
            if placeholders:
                with closing(sqlite3.connect(paths_path)) as connection:
                    rows = connection.execute(
                        "SELECT candidate_id, edge_ids FROM candidate_route_paths "
                        f"WHERE candidate_id IN ({placeholders})",
                        candidate_ids,
                    )
                    stored_paths = {
                        candidate_id: json.loads(zlib.decompress(edge_ids))
                        for candidate_id, edge_ids in rows
                    }
                for route in routes_without_geometry:
                    edge_ids = stored_paths.get(route.get("candidate_id"))
                    if edge_ids:
                        route["legs"] = [{"edge_ids": edge_ids}]
                        route.pop("geometry_approximate", None)

    for route in routes:
        if not route.get("legs"):
            route["geometry_approximate"] = True
    return routes


def _single_agent_routes(
    report: dict[str, Any], scenario_report: dict[str, Any], scenario: str
) -> list[dict[str, Any]]:
    # D4 grava a rota do agente único no topo do cenário, sem lista "routes".
    visit_order = scenario_report.get("visit_order")
    if scenario_report.get("status") != "feasible" or not visit_order:
        return []
    route = {
        "agent_id": 1,
        "visit_order": visit_order,
        "distance_m": scenario_report.get("distance_total_m"),
        "completion_time_s": scenario_report.get("completion_time_s"),
        "repeated_distance_m": scenario_report.get("repeated_distance_m"),
        "scenario": scenario,
    }
    if scenario_report.get("legs"):
        route["legs"] = scenario_report["legs"]
    else:
        route["geometry_approximate"] = True
    return [route]


def build_geojson_file(
    run: ExperimentRun,
    routes: list[dict[str, Any]],
    *,
    cache_key: str,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    output_path = geojson_file_for_run(run, cache_key=cache_key, project_root=project_root)
    root = project_root.resolve()
    with _GEOJSON_LOCK:
        if output_path.is_file():
            return output_path
        road_routes = [
            route
            for route in routes
            if not route.get("geometry_approximate") and _route_has_edges(route)
        ]
        approximate_routes = [
            route
            for route in routes
            if route.get("geometry_approximate") or not _route_has_edges(route)
        ]
        features: list[dict[str, Any]] = []
        if road_routes:
            graph_manifest = json.loads(
                (root / "data/manifests/road-network-mt.json").read_text(encoding="utf-8")
            )
            graph_path = (root / graph_manifest["graph_path"]).resolve()
            if not graph_path.is_relative_to(root) or not graph_path.is_file():
                raise FileNotFoundError("Grafo rodoviário não está disponível")
            features.extend(_route_geojson(road_routes, graph_path)["features"])
        if approximate_routes:
            seats_path = root / "data/processed/municipalities-mt.geojson"
            features.extend(_seat_route_geojson(approximate_routes, seats_path)["features"])
        payload = {"type": "FeatureCollection", "features": features}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_suffix(".geojson.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(output_path)
    return output_path


def _route_has_edges(route: dict[str, Any]) -> bool:
    return any(leg.get("edge_ids") for leg in route.get("legs", []))


def _seat_route_geojson(
    routes: list[dict[str, Any]], seats_path: Path
) -> dict[str, Any]:
    coordinates_by_code = _municipality_coordinates(seats_path)
    features = []
    for route in routes:
        try:
            coordinates = [
                coordinates_by_code[int(code)] for code in route.get("visit_order", [])
            ]
        except KeyError as error:
            raise ValueError(f"Sede ausente do GeoJSON municipal: {error.args[0]}") from error
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
        properties["geometry_approximate"] = True
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "properties": properties,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def geojson_file_for_run(
    run: ExperimentRun,
    *,
    cache_key: str,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    root = project_root.resolve()
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:20]
    return root / "data/processed/jobs" / f"{run.id}-{digest}.geojson"


@lru_cache(maxsize=2)
def _read_report_cached(
    path: str, modified_ns: int, changed_ns: int, size: int
) -> dict[str, Any]:
    del modified_ns, changed_ns, size
    with Path(path).open("r", encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, dict):
        raise ValueError("Relatório do experimento inválido")
    return payload


def _read_report(run: ExperimentRun) -> dict[str, Any]:
    report_path = result_file_for_run(run)
    stat = report_path.stat()
    with _REPORT_CACHE_LOCK:
        return _read_report_cached(
            str(report_path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
        )


def _municipality_coordinates(path: Path) -> dict[int, list[float]]:
    stat = path.stat()
    return _municipality_coordinates_cached(
        str(path.resolve()), stat.st_mtime_ns, stat.st_size
    )


@lru_cache(maxsize=1)
def _municipality_coordinates_cached(
    path: str, modified_ns: int, size: int
) -> dict[int, list[float]]:
    del modified_ns, size
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        int(feature["properties"]["ibge_code"]): feature["geometry"]["coordinates"]
        for feature in payload.get("features", [])
        if feature.get("geometry", {}).get("type") == "Point"
    }


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


def _route_geojson(routes: list[dict[str, Any]], graph_path: Path) -> dict[str, Any]:
    wanted_edge_ids = {
        edge_id
        for route in routes
        for leg in route.get("legs", [])
        for edge_id in leg.get("edge_ids", [])
    }
    if not wanted_edge_ids:
        return {"type": "FeatureCollection", "features": []}

    wanted_endpoints: dict[str, tuple[int, int]] = {}
    with gzip.open(graph_path, "rb") as source:
        for line in source:
            if b'"type":"edge"' not in line:
                continue
            edge = json.loads(line)
            edge_id = edge["edge_id"]
            if edge_id not in wanted_edge_ids:
                continue
            wanted_endpoints[edge_id] = (int(edge["source"]), int(edge["target"]))
            if len(wanted_endpoints) == len(wanted_edge_ids):
                break
    missing = wanted_edge_ids - wanted_endpoints.keys()
    if missing:
        raise ValueError(f"Arestas da rota ausentes no grafo: {len(missing)}")

    needed_node_ids = {
        node_id
        for endpoints in wanted_endpoints.values()
        for node_id in endpoints
    }
    node_coordinates: dict[int, tuple[float, float]] = {}
    with gzip.open(graph_path, "rb") as source:
        for line in source:
            if b'"type":"node"' not in line:
                continue
            node = json.loads(line)
            node_id = int(node["osm_id"])
            if node_id not in needed_node_ids:
                continue
            node_coordinates[node_id] = (
                float(node["longitude"]),
                float(node["latitude"]),
            )
            needed_node_ids.remove(node_id)
            if not needed_node_ids:
                break
    if needed_node_ids:
        raise ValueError(f"Nós da rota ausentes no grafo: {len(needed_node_ids)}")

    edge_coordinates = {
        edge_id: (node_coordinates[source], node_coordinates[target])
        for edge_id, (source, target) in wanted_endpoints.items()
    }
    del node_coordinates, wanted_endpoints

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
