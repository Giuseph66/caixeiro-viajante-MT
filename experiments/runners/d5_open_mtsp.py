from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import multiprocessing
import os
import resource
import sys
import threading
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from memory_limit import enforce_memory_limit

MEMORY_LIMIT = enforce_memory_limit()
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.optimization.visitation.mtsp import (  # noqa: E402
    MTSP_ALGORITHM_VERSION,
    MtspSolution,
    solve_open_mtsp,
)
from app.routing.matrices import MetricMatrix, SeatSnap  # noqa: E402

CUIABA_IBGE_CODE = 5103403
BASE_SEED = 20261008
TOLERANCE = 1e-3


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _scenarios() -> tuple[tuple[str, int, str, int | None], ...]:
    return tuple(
        (
            f"{origin_label}-k{vehicle_count}-{objective}",
            vehicle_count,
            objective,
            origin_code,
        )
        for origin_label, origin_code in (
            ("O1-fixed-cuiaba", CUIABA_IBGE_CODE),
            ("O2-variable-origin", None),
        )
        for vehicle_count in (2, 3)
        for objective in ("distance", "time")
    )


SCENARIOS = _scenarios()


def _memory_cgroup_path() -> Path:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        hierarchy, _controllers, group_path = line.split(":", 2)
        if hierarchy == "0":
            return Path("/sys/fs/cgroup") / group_path.lstrip("/")
    raise RuntimeError("Cgroup v2 não encontrado")


def _memory_current_bytes() -> int:
    return int((_memory_cgroup_path() / "memory.current").read_text().strip())


def _sample_memory_peak(stop: threading.Event, peak_bytes: list[int]) -> None:
    while not stop.wait(0.25):
        peak_bytes[0] = max(peak_bytes[0], _memory_current_bytes())


def _read_edge_metrics(
    graph_path: Path, wanted_edge_ids: set[str]
) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not wanted_edge_ids:
        return found
    print(
        f"Lendo em streaming os custos físicos de {len(wanted_edge_ids):,} arestas.",
        flush=True,
    )
    started = perf_counter()
    with gzip.open(graph_path, "rb") as source:
        for line in source:
            if b'"type":"edge"' not in line:
                continue
            record = json.loads(line)
            edge_id = record["edge_id"]
            if edge_id not in wanted_edge_ids:
                continue
            found[edge_id] = {
                "source": record["source"],
                "target": record["target"],
                "physical_segment_id": record["physical_segment_id"],
                "length_m": record["length_m"],
                "travel_time_s": record["travel_time_s"],
            }
            if len(found) == len(wanted_edge_ids):
                break
    missing = wanted_edge_ids - found.keys()
    if missing:
        raise ValueError(f"Arestas de rota ausentes no grafo: {len(missing)}")
    print(
        f"Custos físicos validados em {perf_counter() - started:.1f}s.",
        flush=True,
    )
    return found


def _serialize_solution(
    solution: MtspSolution,
    scenario: str,
    time_limit_s: float,
) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "status": solution.status,
        "method": solution.method,
        "objective": solution.objective,
        "unit": solution.unit,
        "origin_policy": solution.origin_policy,
        "origin_ibge_code": solution.origin_ibge_code,
        "vehicle_count": solution.vehicle_count,
        "objective_value": solution.objective_value,
        "time_limit_s_per_scenario": time_limit_s,
        "runtime_s": solution.runtime_s,
        "seed": solution.seed,
        "candidate_origins_evaluated": solution.candidate_origins_evaluated,
        "candidate_models_evaluated": solution.candidate_models_evaluated,
        "candidate_assignments_evaluated": solution.candidate_assignments_evaluated,
        "solver_status_code_counts": dict(
            Counter(str(status) for status in solution.solver_statuses)
        ),
        "heuristic": {
            "origin_ibge_code": solution.heuristic_origin_ibge_code,
            "objective_value": solution.heuristic_objective_value,
            "route_objective_values": list(solution.heuristic_route_objective_values),
        },
        "ortools_candidate": {
            "origin_ibge_code": solution.ortools_origin_ibge_code,
            "objective_value": solution.ortools_objective_value,
            "route_objective_values": list(solution.ortools_route_objective_values),
        },
        "routes": [
            {
                "agent_id": route.agent_id,
                "visit_order": list(route.visit_order),
                "objective_value": route.objective_value,
                "matrix_travel_cost": route.travel_cost,
                "service_time_s": route.service_time_s,
                "legs": [
                    {
                        "from_ibge_code": leg.from_ibge_code,
                        "to_ibge_code": leg.to_ibge_code,
                        "matrix_travel_cost": leg.travel_cost,
                        "service_time_s": leg.service_time_s,
                        "edge_ids": list(leg.edge_ids),
                    }
                    for leg in route.legs
                ],
            }
            for route in solution.routes
        ],
    }


def _matrix_without_paths(matrix: MetricMatrix) -> MetricMatrix:
    empty_path: list[str] = []
    paths = [
        [None if cost is None else empty_path for cost in row]
        for row in matrix.costs
    ]
    return MetricMatrix(
        metric=matrix.metric,
        unit=matrix.unit,
        costs=matrix.costs,
        paths=paths,
    )


def _restore_edge_paths(
    scenario: dict[str, Any], matrix: MetricMatrix, seats: list[SeatSnap]
) -> None:
    code_to_index = {seat.ibge_code: index for index, seat in enumerate(seats)}
    for route in scenario["routes"]:
        for leg in route["legs"]:
            from_index = code_to_index[leg["from_ibge_code"]]
            to_index = code_to_index[leg["to_ibge_code"]]
            path = matrix.paths[from_index][to_index]
            if path is None:
                raise ValueError("Solver recebeu um trecho sem caminho")
            leg["edge_ids"] = list(path)


def _load_summary(loads: list[float]) -> dict[str, float | None]:
    if not loads:
        return {"mean": None, "population_stddev": None, "coefficient_of_variation": None}
    mean = sum(loads) / len(loads)
    variance = sum((value - mean) ** 2 for value in loads) / len(loads)
    stddev = math.sqrt(variance)
    return {
        "mean": mean,
        "population_stddev": stddev,
        "coefficient_of_variation": stddev / mean if mean else None,
        "minimum": min(loads),
        "maximum": max(loads),
        "maximum_to_minimum_ratio": max(loads) / min(loads) if min(loads) else None,
    }


def _finalize_scenario(
    scenario: dict[str, Any],
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    edge_metrics: dict[str, dict[str, Any]],
) -> None:
    if scenario["status"] != "feasible" or len(scenario["routes"]) != scenario["vehicle_count"]:
        raise ValueError(f"Solução ausente ou incompleta em {scenario['scenario']}")
    code_to_seat = {seat.ibge_code: seat for seat in seats}
    code_to_index = {seat.ibge_code: index for index, seat in enumerate(seats)}
    expected = set(code_to_seat)
    served: list[int] = []
    total_distance_m = 0.0
    route_times_s: list[float] = []
    objective_loads: list[float] = []
    directed_edge_count = 0
    wanted_origin = scenario["origin_ibge_code"]

    for route in scenario["routes"]:
        order = route["visit_order"]
        if len(order) < 2 or order[0] != wanted_origin or order[-1] == wanted_origin:
            raise ValueError(f"Rota aberta inválida em {scenario['scenario']}")
        served.extend(order[1:])
        route_distance_m = 0.0
        road_time_s = 0.0
        for leg in route["legs"]:
            from_seat = code_to_seat[leg["from_ibge_code"]]
            to_seat = code_to_seat[leg["to_ibge_code"]]
            from_index = code_to_index[from_seat.ibge_code]
            to_index = code_to_index[to_seat.ibge_code]
            expected_cost = matrix.costs[from_index][to_index]
            expected_path = matrix.paths[from_index][to_index]
            if (
                expected_cost is None
                or expected_path is None
                or not math.isclose(
                    leg["matrix_travel_cost"], expected_cost, rel_tol=1e-9, abs_tol=TOLERANCE
                )
                or leg["edge_ids"] != expected_path
            ):
                raise ValueError(f"Caminho/custo da matriz divergente em {scenario['scenario']}")

            physical_edges = [edge_metrics[edge_id] for edge_id in leg["edge_ids"]]
            if physical_edges:
                if physical_edges[0]["source"] != from_seat.node_id:
                    raise ValueError("Caminho não começa no nó de encaixe da sede")
                if physical_edges[-1]["target"] != to_seat.node_id:
                    raise ValueError("Caminho não termina no nó de encaixe da sede")
                if any(
                    left["target"] != right["source"]
                    for left, right in zip(physical_edges, physical_edges[1:])
                ):
                    raise ValueError("Arestas da trajetória não são contínuas")
            elif from_seat.node_id != to_seat.node_id:
                raise ValueError("Caminho vazio entre nós rodoviários diferentes")

            distance = sum(edge["length_m"] for edge in physical_edges)
            travel_time = sum(edge["travel_time_s"] for edge in physical_edges)
            route_distance_m += distance
            road_time_s += travel_time
            directed_edge_count += len(physical_edges)
            leg["distance_m"] = distance
            leg["travel_time_s"] = travel_time
            leg["physical_segment_ids"] = [
                edge["physical_segment_id"] for edge in physical_edges
            ]

        completion_time_s = road_time_s + route["service_time_s"]
        route["distance_m"] = route_distance_m
        route["road_time_s"] = road_time_s
        route["completion_time_s"] = completion_time_s
        route["directed_edge_count"] = sum(
            len(leg["edge_ids"]) for leg in route["legs"]
        )
        route["objective_load"] = (
            route_distance_m
            if scenario["objective"] == "distance"
            else completion_time_s
        )
        if scenario["objective"] == "distance" and not math.isclose(
            route_distance_m,
            route["objective_value"],
            rel_tol=1e-9,
            abs_tol=TOLERANCE,
        ):
            raise ValueError("Distância reconstruída diverge do custo da solução")
        if scenario["objective"] == "time" and not math.isclose(
            completion_time_s,
            route["objective_value"],
            rel_tol=1e-9,
            abs_tol=TOLERANCE,
        ):
            raise ValueError("Tempo reconstruído diverge do custo da solução")
        total_distance_m += route_distance_m
        route_times_s.append(completion_time_s)
        objective_loads.append(route["objective_load"])

    if len(served) != len(expected) - 1 or len(set(served)) != len(served):
        raise ValueError(f"Sede ausente ou duplicada em {scenario['scenario']}")
    if set(served) != expected - {wanted_origin}:
        raise ValueError(f"Cobertura municipal incompleta em {scenario['scenario']}")

    objective_value = (
        total_distance_m
        if scenario["objective"] == "distance"
        else max(route_times_s)
    )
    if not math.isclose(
        objective_value,
        scenario["objective_value"],
        rel_tol=1e-9,
        abs_tol=TOLERANCE,
    ):
        raise ValueError(f"Objetivo reconstruído diverge em {scenario['scenario']}")
    scenario["total_distance_m"] = total_distance_m
    scenario["time_max_s"] = max(route_times_s)
    scenario["objective_loads"] = objective_loads
    scenario["load_balance"] = _load_summary(objective_loads)
    scenario["directed_edge_count"] = directed_edge_count
    scenario["physical_edge_ids_validated"] = True
    scenario["all_municipalities_served_once"] = True
    scenario["all_agents_active"] = all(len(route["visit_order"]) >= 2 for route in scenario["routes"])
    scenario["open_routes"] = all(
        route["visit_order"][-1] != wanted_origin for route in scenario["routes"]
    )


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Executa cenários reais open mTSP D5 com teto de RAM de 25 GB."
    )
    parser.add_argument("--time-limit-s", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=BASE_SEED)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--matrix-manifest",
        type=Path,
        default=ROOT / "data/manifests/travel-matrix-mt.json",
    )
    parser.add_argument(
        "--graph-manifest",
        type=Path,
        default=ROOT / "data/manifests/road-network-mt.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data/manifests/mtsp-benchmarks-mt.json",
    )
    args = parser.parse_args()
    if not math.isfinite(args.time_limit_s) or args.time_limit_s <= 0:
        parser.error("--time-limit-s deve ser positivo")
    if not 0 <= args.seed <= 2**31 - 1:
        parser.error("--seed deve estar entre 0 e 2^31-1")
    if not 1 <= args.workers <= 8:
        parser.error("--workers deve estar entre 1 e 8")

    matrix_manifest_path = args.matrix_manifest.resolve()
    graph_manifest_path = args.graph_manifest.resolve()
    output_path = args.output.resolve()
    checkpoint_path = ROOT / "data/processed/mtsp-d5-checkpoint.json"
    if output_path.exists():
        raise FileExistsError(f"Relatório já existe; escolha --output: {output_path}")

    matrix_manifest = json.loads(matrix_manifest_path.read_text())
    graph_manifest = json.loads(graph_manifest_path.read_text())
    matrix_path = (ROOT / matrix_manifest["cache_path"]).resolve()
    graph_path = (ROOT / graph_manifest["graph_path"]).resolve()
    matrix_sha256 = _sha256(matrix_path)
    graph_sha256 = _sha256(graph_path)
    if matrix_sha256 != matrix_manifest["cache_sha256"]:
        raise ValueError("SHA-256 do cache de matrizes diverge do manifesto")
    if graph_sha256 != graph_manifest["graph_sha256"]:
        raise ValueError("SHA-256 do grafo diverge do manifesto")
    if graph_sha256 != matrix_manifest["graph_sha256"]:
        raise ValueError("Manifestos do grafo e das matrizes divergem")
    algorithm_source_sha256 = _sha256(
        ROOT / "backend/app/optimization/visitation/mtsp.py"
    )
    fingerprint = {
        "matrix_cache_key": matrix_manifest["cache_key"],
        "matrix_cache_sha256": matrix_sha256,
        "graph_sha256": graph_sha256,
        "algorithm_version": MTSP_ALGORITHM_VERSION,
        "algorithm_source_sha256": algorithm_source_sha256,
        "time_limit_s_per_scenario": args.time_limit_s,
        "seed": args.seed,
        "parallel_solver_processes": args.workers,
        "multiprocessing_start_method": "spawn",
        "scenario_ids": [scenario[0] for scenario in SCENARIOS],
    }

    results: list[dict[str, Any]] = []
    if checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text())
        if checkpoint.get("fingerprint") != fingerprint:
            raise ValueError("Checkpoint D5 pertence a dados, solver ou configuração diferentes")
        results = checkpoint.get("scenarios", [])
    completed = {item["scenario"] for item in results}

    print(
        f"Cache {matrix_path.stat().st_size:,} bytes e grafo {graph_path.stat().st_size:,} bytes; "
        f"hashes validados. Até {args.workers} processos spawn sob teto de 25 GB.",
        flush=True,
    )
    memory_peak_bytes = [_memory_current_bytes()]
    memory_monitor_stop = threading.Event()
    memory_monitor = threading.Thread(
        target=_sample_memory_peak,
        args=(memory_monitor_stop, memory_peak_bytes),
        name="d5-cgroup-memory-monitor",
        daemon=True,
    )
    memory_monitor.start()
    with gzip.open(matrix_path, "rt", encoding="utf-8") as source:
        payload = json.load(source)
    if payload.get("cache_key") != matrix_manifest["cache_key"]:
        raise ValueError("Chave do cache diverge do manifesto")
    seats = [SeatSnap(**item) for item in payload["municipalities"]]
    if len(seats) != matrix_manifest["municipality_count"]:
        raise ValueError("Quantidade de sedes diverge do manifesto D3")
    if CUIABA_IBGE_CODE not in {seat.ibge_code for seat in seats}:
        raise ValueError("Cuiabá não está presente na matriz")
    matrices = {
        metric: MetricMatrix(
            metric=metric,
            unit=payload[metric]["unit"],
            costs=payload[metric]["costs"],
            paths=payload[metric]["paths"],
        )
        for metric in ("distance", "time")
    }
    worker_matrices = {
        metric: _matrix_without_paths(matrix) for metric, matrix in matrices.items()
    }
    pending = [
        (position, scenario)
        for position, scenario in enumerate(SCENARIOS, start=1)
        if scenario[0] not in completed
    ]
    for position, scenario in enumerate(SCENARIOS, start=1):
        if scenario[0] in completed:
            print(
                f"[{position}/{len(SCENARIOS)}] checkpoint: {scenario[0]} pronto.",
                flush=True,
            )

    with ProcessPoolExecutor(
        max_workers=args.workers,
        mp_context=multiprocessing.get_context("spawn"),
    ) as pool:
        futures = {
            pool.submit(
                _run_scenario,
                position,
                scenario,
                seats,
                worker_matrices[scenario[2]],
                args.time_limit_s,
                args.seed + position - 1,
                len(SCENARIOS),
            ): scenario[0]
            for position, scenario in pending
        }
        failures = []
        for future in as_completed(futures):
            try:
                item = future.result()
                _restore_edge_paths(item, matrices[item["objective"]], seats)
            except Exception as exc:
                failures.append((futures[future], exc))
                print(f"Falha em {futures[future]}: {exc}", flush=True)
                continue
            results.append(item)
            completed.add(item["scenario"])
            _write_json(
                checkpoint_path,
                {"fingerprint": fingerprint, "scenarios": results},
            )
        if failures:
            summary = ", ".join(f"{scenario}: {error}" for scenario, error in failures)
            raise RuntimeError(f"Falhas nos cenários D5: {summary}")

    if completed != {scenario[0] for scenario in SCENARIOS}:
        raise RuntimeError("Há cenários D5 ausentes no checkpoint")

    wanted_edge_ids = {
        edge_id
        for item in results
        for route in item["routes"]
        for leg in route["legs"]
        for edge_id in leg["edge_ids"]
    }
    edge_metrics = _read_edge_metrics(graph_path, wanted_edge_ids)
    for item in results:
        _finalize_scenario(item, seats, matrices[item["objective"]], edge_metrics)

    memory_monitor_stop.set()
    memory_monitor.join()
    memory_peak_bytes[0] = max(memory_peak_bytes[0], _memory_current_bytes())
    report = {
        "format_version": 1,
        "dataset": "Open mTSP P1 / D5 for Mato Grosso municipal seats",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "fingerprint": fingerprint,
        "matrix_manifest": str(matrix_manifest_path.relative_to(ROOT)),
        "graph_manifest": str(graph_manifest_path.relative_to(ROOT)),
        "city_count": len(seats),
        "algorithm": "OR-Tools RoutingModel, active vehicles, common start and artificial open ends",
        "configuration": {
            "time_limit_s_per_scenario": args.time_limit_s,
            "variable_origin_time_budget_shared_across_candidates": True,
            "sequential_scenarios": False,
            "parallel_solver_processes": args.workers,
            "parallel_scenario_threads": 1,
            "multiprocessing_start_method": "spawn",
            "available_logical_cpus": os.cpu_count(),
            "memory_limit_bytes": MEMORY_LIMIT,
            "peak_rss_mib": round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1
            ),
            "cgroup_scope_peak_bytes": memory_peak_bytes[0],
            "elapsed_s": round(perf_counter() - run_started, 3),
        },
        "optimality_claim": (
            "real 142-city scenarios are feasible best-known only; "
            "time-limited fixed-origin subproblems do not prove global optimality"
        ),
        "edge_metrics_source": "road-network-mt.jsonl.gz; selected edge IDs streamed without loading the full graph",
        "validated_edge_id_count": len(wanted_edge_ids),
        "scenarios": sorted(results, key=lambda item: item["scenario"]),
    }
    _write_json(output_path, report)
    checkpoint_path.unlink(missing_ok=True)
    print(f"Relatório D5 validado e gravado: {output_path}", flush=True)


def _run_scenario(
    position: int,
    scenario: tuple[str, int, str, int | None],
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    time_limit_s: float,
    seed: int,
    scenario_count: int,
) -> dict[str, Any]:
    scenario_id, vehicle_count, objective, origin_code = scenario
    print(
        f"[{position}/{scenario_count}] iniciando {scenario_id}; "
        f"busca total {time_limit_s:g}s, seed {seed}.",
        flush=True,
    )

    def progress(candidate: int, total: int, candidate_origin: int) -> None:
        if candidate == 1 or candidate % 10 == 0 or candidate == total:
            print(
                f"    {scenario_id}: origens {candidate}/{total}; "
                f"candidata IBGE {candidate_origin}.",
                flush=True,
            )

    started = perf_counter()
    solution = solve_open_mtsp(
        seats,
        matrix,
        vehicle_count=vehicle_count,
        origin_ibge_code=origin_code,
        objective=objective,
        time_limit_s=time_limit_s,
        seed=seed,
        prove_small_instances=False,
        progress_callback=progress,
        include_edge_paths=False,
    )
    item = _serialize_solution(solution, scenario_id, time_limit_s)
    item["wall_time_s"] = perf_counter() - started
    item["origin_policy"] = (
        "fixed_cuiaba" if origin_code is not None else "optimized_among_all_seats"
    )
    print(
        f"[{position}/{scenario_count}] {scenario_id}: {solution.status}, "
        f"{solution.method}, {solution.objective_value}, "
        f"{solution.candidate_origins_evaluated} origens, "
        f"{solution.runtime_s:.1f}s solver / {item['wall_time_s']:.1f}s parede.",
        flush=True,
    )
    return item


if __name__ == "__main__":
    run_started = perf_counter()
    _main()
