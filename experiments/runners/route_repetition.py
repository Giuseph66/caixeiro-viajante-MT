from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

from app.optimization.overlap import PhysicalEdgeMetric
from app.optimization.repetition import (
    RepetitionPolicy,
    refine_route_repetition,
    repeated_distance,
    repetition_penalty,
)
from app.routing.matrices import MetricMatrix, SeatSnap


def load_physical_edges(
    graph_path: Path,
    *,
    matrices: Iterable[MetricMatrix] = (),
    selected_edge_ids: Iterable[str] = (),
) -> dict[str, PhysicalEdgeMetric]:
    """Stream only required edge metadata; never load another road graph."""
    wanted = set(selected_edge_ids)
    for matrix in matrices:
        for row in matrix.paths:
            for path in row:
                if path:
                    wanted.update(path)
    found: dict[str, PhysicalEdgeMetric] = {}
    print(f"Repetição: lendo métricas de {len(wanted):,} arestas físicas.", flush=True)
    if wanted:
        with gzip.open(graph_path, "rb") as source:
            for line in source:
                if b'"type":"edge"' not in line:
                    continue
                record = json.loads(line)
                edge_id = record["edge_id"]
                if edge_id not in wanted:
                    continue
                metric = PhysicalEdgeMetric(
                    edge_id=edge_id,
                    physical_segment_id=record["physical_segment_id"],
                    length_m=float(record["length_m"]),
                    source_node=record["source"],
                    target_node=record["target"],
                    travel_time_s=float(record["travel_time_s"]),
                )
                if not math.isfinite(metric.travel_time_s) or metric.travel_time_s < 0:
                    raise ValueError(f"Tempo físico inválido: {edge_id}")
                found[edge_id] = metric
                if len(found) == len(wanted):
                    break
    if wanted - found.keys():
        raise ValueError("Arestas das matrizes ausentes no grafo rodoviário")
    return found


def apply_repetition_rule(
    scenario: dict[str, Any],
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    edge_metrics: dict[str, PhysicalEdgeMetric],
    *,
    policy: RepetitionPolicy,
    time_limit_s: float,
    seed: int,
    keep_rejected_routes: bool = False,
    preview_path: Path | None = None,
) -> None:
    routes = scenario.get("routes", [scenario])
    if not routes or not all(route.get("visit_order") for route in routes):
        return
    print(f"Repetição: aplicando {policy} em {scenario['scenario']}.", flush=True)
    services = {
        int(leg["to_ibge_code"]): float(leg.get("service_time_s", 0))
        for route in routes
        for leg in route.get("legs", [])
    }
    last_preview_at = 0.0

    def publish(orders: tuple[tuple[int, ...], ...], value: float) -> None:
        nonlocal last_preview_at
        if preview_path is None or perf_counter() - last_preview_at < 0.5:
            return
        last_preview_at = perf_counter()
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = preview_path.with_name(preview_path.name + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "scenario": scenario["scenario"],
                    "objective_value": value,
                    "repetition_policy": policy,
                    "routes": [
                        {"agent_id": i, "visit_order": order}
                        for i, order in enumerate(orders, start=1)
                    ],
                }
            ),
            encoding="utf-8",
        )
        temporary.replace(preview_path)

    result = refine_route_repetition(
        [route["visit_order"] for route in routes],
        [seat.ibge_code for seat in seats],
        matrix,
        edge_metrics,
        policy=policy,
        time_limit_s=time_limit_s,
        fixed_origin=scenario.get("origin_policy")
        not in ("optimized", "optimized_among_all_seats"),
        destination_ibge_code=scenario.get("destination_ibge_code"),
        service_times_s=services,
        seed=seed,
        progress_callback=publish,
    )
    if policy != "allow":
        indices = {seat.ibge_code: i for i, seat in enumerate(seats)}
        for route, order, repeats in zip(
            routes, result.orders, result.repeated_distance_by_agent_m, strict=True
        ):
            legs = []
            for a, b in zip(order, order[1:]):
                i, j = indices[a], indices[b]
                path, cost = matrix.paths[i][j], matrix.costs[i][j]
                if path is None or cost is None:
                    raise ValueError("Busca de repetição produziu perna desconectada")
                legs.append(
                    {
                        "from_ibge_code": a,
                        "to_ibge_code": b,
                        "edge_ids": list(path),
                        "travel_cost": cost,
                        "matrix_travel_cost": cost,
                        "service_time_s": services.get(b, 0.0),
                    }
                )
            physical = [edge_metrics[e] for leg in legs for e in leg["edge_ids"]]
            travel = sum(leg["travel_cost"] for leg in legs)
            service = sum(leg["service_time_s"] for leg in legs)
            route.update(
                visit_order=list(order),
                legs=legs,
                travel_cost=travel,
                objective_value=travel + (service if matrix.metric == "time" else 0),
                service_time_s=service,
                total_service_time_s=service,
                distance_m=sum(edge.length_m for edge in physical),
                completion_time_s=sum(edge.travel_time_s for edge in physical)
                + service,
                repeated_distance_m=repeats,
            )
        scenario.update(
            origin_ibge_code=result.orders[0][0],
            objective_value=result.objective_value,
            method=f"{scenario.get('method', 'candidate_pool')}+repetition_search",
        )
        scenario["runtime_s"] = float(scenario.get("runtime_s", 0)) + result.runtime_s

    for route in routes:
        route["repeated_distance_m"] = repeated_distance(
            [e for leg in route.get("legs", []) for e in leg["edge_ids"]],
            edge_metrics,
        )
    duplicate_total = sum(route["repeated_distance_m"] for route in routes)
    feasible = policy != "forbid" or duplicate_total <= 1e-6
    penalty = repetition_penalty(duplicate_total, matrix.metric, policy)
    if policy != "allow":
        scenario["objective_value"] = result.base_objective + penalty
    scenario.update(
        repetition_policy=policy,
        repeated_distance_m=duplicate_total,
        repeated_distance_by_agent_m=[route["repeated_distance_m"] for route in routes],
        repetition_penalty_value=penalty,
        base_objective_value=result.base_objective,
        repetition_search={
            "candidates_evaluated": result.candidates_evaluated,
            "runtime_s": result.runtime_s,
            "feasible": feasible,
            "proof_scope": "directed matrix paths; heuristic city-order search",
            "best_repeated_distance_m": duplicate_total,
        },
    )
    if "routes" not in scenario and policy != "allow":
        route = routes[0]
        scenario.update(
            leg_count=len(route["legs"]),
            directed_edge_count=sum(len(leg["edge_ids"]) for leg in route["legs"]),
            distance_total_m=route["distance_m"],
            total_distance_m=route["distance_m"],
            distance_by_agent_m=[route["distance_m"]],
            max_agent_distance_m=route["distance_m"],
            completion_time_by_agent_s=[route["completion_time_s"]],
            time_max_s=route["completion_time_s"],
        )
    if not feasible and not keep_rejected_routes:
        scenario.update(
            status="not_found",
            objective_value=None,
            total_distance_m=None,
            distance_total_m=None,
            time_max_s=None,
            repeated_distance_m=None,
        )
        scenario.update(
            base_objective_value=None,
            repetition_penalty_value=None,
            repeated_distance_by_agent_m=[],
            distance_by_agent_m=[],
            completion_time_by_agent_s=[],
            max_agent_distance_m=None,
            completion_time_s=None,
            travel_cost=None,
            total_service_time_s=0.0,
            municipalities_served=0,
            municipalities_by_agent=[],
            leg_count=0,
            directed_edge_count=0,
        )
        if "routes" in scenario:
            scenario["routes"] = []
        else:
            scenario.update(visit_order=[], legs=[])
    if preview_path is not None:
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview = {
            "scenario": scenario["scenario"],
            "objective_value": scenario.get("objective_value"),
            "candidate_count": result.candidates_evaluated,
            "repetition_policy": policy,
            "routes": [
                {"agent_id": i, "visit_order": route["visit_order"]}
                for i, route in enumerate(routes, start=1)
            ]
            if feasible
            else [],
        }
        temporary = preview_path.with_name(preview_path.name + ".tmp")
        temporary.write_text(json.dumps(preview, ensure_ascii=False), encoding="utf-8")
        temporary.replace(preview_path)
