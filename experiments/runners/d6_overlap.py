from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import multiprocessing
import os
import sys
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from memory_limit import enforce_memory_limit

MEMORY_LIMIT = enforce_memory_limit()
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.optimization.overlap import (  # noqa: E402
    PhysicalEdgeMetric,
    RouteCandidate,
    assess_policy,
    solve_route_pool,
)
from app.optimization.visitation.mtsp import (  # noqa: E402
    MTSP_ALGORITHM_VERSION,
    MtspSolution,
    solve_open_mtsp,
)
from app.routing.matrices import MetricMatrix, SeatSnap  # noqa: E402

BASE_SEED = 20261008
CUIABA_IBGE_CODE = 5103403
DEFAULT_LAMBDAS = (0.0, 0.25, 0.5, 1.0, 2.0)
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


def _matrix_without_paths(matrix: MetricMatrix) -> MetricMatrix:
    empty_path: list[str] = []
    return MetricMatrix(
        metric=matrix.metric,
        unit=matrix.unit,
        costs=matrix.costs,
        paths=[
            [None if cost is None else empty_path for cost in row]
            for row in matrix.costs
        ],
    )


def _scenario_specs(
    scenarios: list[dict[str, Any]],
) -> tuple[tuple[str, int, str, int | None], ...]:
    output = []
    for item in sorted(scenarios, key=lambda scenario: scenario["scenario"]):
        origin = (
            CUIABA_IBGE_CODE
            if item["origin_policy"] == "fixed_cuiaba"
            else None
        )
        output.append(
            (
                item["scenario"],
                int(item["vehicle_count"]),
                item["objective"],
                origin,
            )
        )
    return tuple(output)


def _serialize_solution(
    solution: MtspSolution,
    *,
    scenario: str,
    variant_id: str,
    seed: int,
) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "variant_id": variant_id,
        "seed": seed,
        "status": solution.status,
        "method": solution.method,
        "objective_value": solution.objective_value,
        "origin_ibge_code": solution.origin_ibge_code,
        "candidate_origins_evaluated": solution.candidate_origins_evaluated,
        "routes": [
            {
                "agent_id": route.agent_id,
                "visit_order": list(route.visit_order),
                "distance_m": None,
                "completion_time_s": None,
                "service_time_s": route.service_time_s,
                "legs": [
                    {
                        "from_ibge_code": leg.from_ibge_code,
                        "to_ibge_code": leg.to_ibge_code,
                        "travel_cost": leg.travel_cost,
                        "service_time_s": leg.service_time_s,
                        "edge_ids": list(leg.edge_ids),
                    }
                    for leg in route.legs
                ],
            }
            for route in solution.routes
        ],
    }


def _run_variant(
    position: int,
    scenario: tuple[str, int, str, int | None],
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    time_limit_s: float,
    seed: int,
    task_count: int,
) -> dict[str, Any]:
    scenario_id, vehicle_count, objective, origin_code = scenario
    variant_id = f"{scenario_id}:seed-{seed}"
    print(
        f"[{position}/{task_count}] D6 iniciando {variant_id} "
        f"(limite {time_limit_s:g}s).",
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
        include_edge_paths=False,
    )
    result = _serialize_solution(
        solution,
        scenario=scenario_id,
        variant_id=variant_id,
        seed=seed,
    )
    result["wall_time_s"] = round(perf_counter() - started, 3)
    if result["status"] != "feasible":
        raise RuntimeError(
            f"D6 não gerou rota viável para {variant_id}: {result['status']}"
        )
    print(
        f"[{position}/{task_count}] D6 concluiu {variant_id}; "
        f"origem {result['origin_ibge_code']}; "
        f"{result['wall_time_s']:.1f}s.",
        flush=True,
    )
    return result


def _restore_paths(
    solution: dict[str, Any],
    matrix: MetricMatrix,
    seats_by_code: dict[int, SeatSnap],
) -> None:
    code_to_index = {code: index for index, code in enumerate(seats_by_code)}
    for route in solution["routes"]:
        visit_order = route["visit_order"]
        if len(route["legs"]) != len(visit_order) - 1:
            raise ValueError(f"Quantidade de pernas incorreta em {solution['variant_id']}")
        for leg in route["legs"]:
            from_code = int(leg["from_ibge_code"])
            to_code = int(leg["to_ibge_code"])
            try:
                from_index = code_to_index[from_code]
                to_index = code_to_index[to_code]
            except KeyError as exc:
                raise ValueError(f"Sede ausente da matriz: {exc.args[0]}") from exc
            expected_path = matrix.paths[from_index][to_index]
            expected_cost = matrix.costs[from_index][to_index]
            if expected_path is None or expected_cost is None:
                raise ValueError(f"Perna sem caminho dirigido: {from_code}->{to_code}")
            observed_path = tuple(leg.get("edge_ids", ()))
            if observed_path and observed_path != tuple(expected_path):
                raise ValueError("Caminho de rota diverge do cache dirigido")
            leg["edge_ids"] = list(expected_path)
            observed_cost = float(leg["travel_cost"])
            if not math.isclose(
                observed_cost, float(expected_cost), rel_tol=1e-9, abs_tol=TOLERANCE
            ):
                raise ValueError(f"Custo da perna diverge da matriz: {from_code}->{to_code}")


def _restore_d5_solution(
    scenario: dict[str, Any],
    matrix: MetricMatrix,
    seats_by_code: dict[int, SeatSnap],
) -> dict[str, Any]:
    solution = {
        "scenario": scenario["scenario"],
        "variant_id": f"{scenario['scenario']}:d5",
        "seed": scenario["seed"],
        "status": scenario["status"],
        "origin_ibge_code": scenario["origin_ibge_code"],
        "routes": [
            {
                **route,
                "legs": [
                    {
                        **leg,
                        "travel_cost": leg["matrix_travel_cost"],
                    }
                    for leg in route["legs"]
                ],
            }
            for route in scenario["routes"]
        ],
    }
    _restore_paths(solution, matrix, seats_by_code)
    return solution


def _read_edge_metrics(
    graph_path: Path,
    wanted_edge_ids: set[str],
) -> dict[str, PhysicalEdgeMetric]:
    found: dict[str, PhysicalEdgeMetric] = {}
    if not wanted_edge_ids:
        return found
    print(
        f"Auditoria D6 em streaming: {len(wanted_edge_ids):,} arestas físicas.",
        flush=True,
    )
    with gzip.open(graph_path, "rb") as source:
        for line in source:
            if b'"type":"edge"' not in line:
                continue
            record = json.loads(line)
            edge_id = record["edge_id"]
            if edge_id not in wanted_edge_ids:
                continue
            found[edge_id] = PhysicalEdgeMetric(
                edge_id=edge_id,
                physical_segment_id=record["physical_segment_id"],
                length_m=float(record["length_m"]),
                source_node=int(record["source"]),
                target_node=int(record["target"]),
                travel_time_s=float(record["travel_time_s"]),
            )
            if len(found) == len(wanted_edge_ids):
                break
    missing = wanted_edge_ids - found.keys()
    if missing:
        raise ValueError(f"Arestas de rota ausentes no grafo: {len(missing)}")
    return found


def _route_candidate(
    solution: dict[str, Any],
    route: dict[str, Any],
    seats_by_code: dict[int, SeatSnap],
    edge_metrics: dict[str, PhysicalEdgeMetric],
) -> tuple[RouteCandidate, dict[str, Any]]:
    visit_order = tuple(int(code) for code in route["visit_order"])
    if len(visit_order) < 2 or visit_order[-1] == visit_order[0]:
        raise ValueError(f"Rota não está aberta: {solution['variant_id']}")
    legs = route["legs"]
    if len(legs) != len(visit_order) - 1:
        raise ValueError(f"Rota sem uma perna por visita em {solution['variant_id']}")

    edge_ids: list[str] = []
    distance_m = 0.0
    road_time_s = 0.0
    service_time_s = 0.0
    for leg, from_code, to_code in zip(
        legs, visit_order[:-1], visit_order[1:], strict=True
    ):
        if (
            int(leg["from_ibge_code"]) != from_code
            or int(leg["to_ibge_code"]) != to_code
        ):
            raise ValueError("Sequência de pernas diverge da ordem de visita")
        try:
            cursor = seats_by_code[from_code].node_id
            target_node = seats_by_code[to_code].node_id
        except KeyError as exc:
            raise ValueError(f"Sede ausente da matriz: {exc.args[0]}") from exc
        for edge_id in leg["edge_ids"]:
            metric = edge_metrics.get(edge_id)
            if metric is None:
                raise ValueError(f"Métrica ausente para {edge_id!r}")
            if metric.source_node != cursor or metric.target_node is None:
                raise ValueError(f"Caminho desconectado na aresta {edge_id!r}")
            cursor = metric.target_node
            distance_m += metric.length_m
            if metric.travel_time_s is None:
                raise ValueError(f"Tempo ausente para a aresta {edge_id!r}")
            road_time_s += metric.travel_time_s
            edge_ids.append(edge_id)
        if cursor != target_node:
            raise ValueError(f"Caminho não termina na sede IBGE {to_code}")
        service_time_s += float(leg.get("service_time_s", 0.0))

    candidate_id = f"{solution['variant_id']}:agent-{route['agent_id']}"
    candidate = RouteCandidate(
        candidate_id=candidate_id,
        agent_id=int(route["agent_id"]),
        served_city_codes=visit_order[1:],
        edge_ids=tuple(edge_ids),
        distance_m=distance_m,
        completion_time_s=road_time_s + service_time_s,
    )
    return candidate, {
        "visit_order": list(visit_order),
        "distance_m": distance_m,
        "road_time_s": road_time_s,
        "service_time_s": service_time_s,
    }


def _candidate_summary(
    route: RouteCandidate,
    route_info: dict[str, Any],
    edge_metrics: dict[str, PhysicalEdgeMetric],
) -> dict[str, Any]:
    digest = hashlib.sha256("\n".join(route.edge_ids).encode()).hexdigest()
    physical_ids = sorted(
        {edge_metrics[edge_id].physical_segment_id for edge_id in route.edge_ids}
    )
    physical_digest = hashlib.sha256("\n".join(physical_ids).encode()).hexdigest()
    return {
        "candidate_id": route.candidate_id,
        "agent_id": route.agent_id,
        "visit_order": route_info["visit_order"],
        "served_city_count": len(route.served_city_codes),
        "distance_m": route.distance_m,
        "completion_time_s": route.completion_time_s,
        "edge_count": len(route.edge_ids),
        "edge_ids_sha256": digest,
        "physical_segment_count": len(physical_ids),
        "physical_segment_ids_sha256": physical_digest,
    }


def _pool_summary(
    solution: Any,
    route_info: dict[str, dict[str, Any]],
    edge_metrics: dict[str, PhysicalEdgeMetric],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": solution.status,
        "proof_scope": solution.proof_scope,
        "complete_combinations_examined": solution.complete_combinations_examined,
    }
    if solution.assessment is None:
        return result
    assessment = solution.assessment
    result.update(
        {
            "objective": assessment.objective,
            "policy": assessment.policy,
            "base_objective": assessment.base_objective,
            "overlap_distance_m": assessment.overlap.redundant_distance_m,
            "shared_segment_count": assessment.overlap.shared_segment_count,
            "penalty_value": assessment.penalty_value,
            "objective_value": assessment.objective_value,
            "feasible": assessment.feasible,
            "shared_segments": [
                asdict(segment) for segment in assessment.overlap.shared_segments
            ],
            "routes": [
                _candidate_summary(route, route_info[route.candidate_id], edge_metrics)
                for route in solution.routes
            ],
        }
    )
    return result


def _build_scenario_report(
    scenario: dict[str, Any],
    solutions: list[dict[str, Any]],
    seats: list[SeatSnap],
    edge_metrics: dict[str, PhysicalEdgeMetric],
    lambdas: tuple[float, ...],
    reference_speed_kph: float,
) -> dict[str, Any]:
    seats_by_code = {seat.ibge_code: seat for seat in seats}
    required_all = set(seats_by_code)
    origin_solutions: dict[int, list[dict[str, Any]]] = {}
    for solution in solutions:
        origin = int(solution["origin_ibge_code"])
        origin_solutions.setdefault(origin, []).append(solution)

    origin_reports: list[dict[str, Any]] = []
    for origin, origin_group in sorted(origin_solutions.items()):
        candidates: list[RouteCandidate] = []
        route_info: dict[str, dict[str, Any]] = {}
        seen_routes: set[tuple[int, tuple[int, ...], tuple[str, ...]]] = set()
        solution_ids: list[str] = []
        d5_candidates: list[RouteCandidate] = []
        for solution in sorted(origin_group, key=lambda item: item["variant_id"]):
            solution_ids.append(solution["variant_id"])
            for route in solution["routes"]:
                candidate, details = _route_candidate(
                    solution, route, seats_by_code, edge_metrics
                )
                signature = (
                    candidate.agent_id,
                    candidate.served_city_codes,
                    candidate.edge_ids,
                )
                if signature in seen_routes:
                    continue
                seen_routes.add(signature)
                candidates.append(candidate)
                route_info[candidate.candidate_id] = details
                if solution["variant_id"].endswith(":d5"):
                    d5_candidates.append(candidate)

        vehicle_count = int(scenario["vehicle_count"])
        agent_ids = tuple(sorted({candidate.agent_id for candidate in candidates}))
        if len(agent_ids) != vehicle_count:
            raise ValueError(
                f"{scenario['scenario']} em IBGE {origin}: esperados "
                f"{vehicle_count} agentes ativos, encontrados {agent_ids}"
            )
        required = sorted(required_all - {origin})
        policy_args = {
            "required_city_codes": required,
            "agent_ids": agent_ids,
            "edge_metrics": edge_metrics,
            "objective": scenario["objective"],
            "candidate_set_complete": False,
        }
        p1_solution = solve_route_pool(
            candidates,
            policy="P1",
            reference_speed_kph=reference_speed_kph,
            **policy_args,
        )
        p1_summary = _pool_summary(p1_solution, route_info, edge_metrics)
        p2_solutions = []
        p2_summaries = []
        for penalty_lambda in lambdas:
            p2_solution = solve_route_pool(
                candidates,
                policy="P2",
                penalty_lambda=penalty_lambda,
                reference_speed_kph=reference_speed_kph,
                **policy_args,
            )
            p2_summary = _pool_summary(p2_solution, route_info, edge_metrics)
            p2_summary["lambda"] = penalty_lambda
            p2_solutions.append(p2_solution)
            p2_summaries.append(p2_summary)
        zero_index = lambdas.index(0.0)
        if tuple(route.candidate_id for route in p1_solution.routes) != tuple(
            route.candidate_id for route in p2_solutions[zero_index].routes
        ):
            raise ValueError("P2 com lambda zero divergiu do baseline P1")
        p3_solution = solve_route_pool(
            candidates,
            policy="P3",
            reference_speed_kph=reference_speed_kph,
            **policy_args,
        )
        p3_summary = _pool_summary(p3_solution, route_info, edge_metrics)

        d5_reference = None
        if len(d5_candidates) == vehicle_count:
            assessment = assess_policy(
                d5_candidates,
                edge_metrics,
                objective=scenario["objective"],
                policy="P1",
                reference_speed_kph=reference_speed_kph,
            )
            d5_reference = {
                "objective_value": assessment.base_objective,
                "overlap_distance_m": assessment.overlap.redundant_distance_m,
                "shared_segment_count": assessment.overlap.shared_segment_count,
                "shared_segments": [
                    asdict(segment) for segment in assessment.overlap.shared_segments
                ],
                "candidate_ids": [route.candidate_id for route in d5_candidates],
            }

        origin_reports.append(
            {
                "origin_ibge_code": origin,
                "candidate_solution_count": len(solution_ids),
                "candidate_solution_ids": solution_ids,
                "route_candidate_count_by_agent": {
                    str(agent_id): sum(
                        candidate.agent_id == agent_id for candidate in candidates
                    )
                    for agent_id in agent_ids
                },
                "d5_reference": d5_reference,
                "p1": p1_summary,
                "p2": p2_summaries,
                "p3": p3_summary,
            }
        )

    def best_summary(
        field: str,
        *,
        lambda_value: float | None = None,
        p3: bool = False,
    ) -> dict[str, Any] | None:
        options = []
        for report in origin_reports:
            summary = report[field]
            if field == "p2":
                summary = next(item for item in summary if item["lambda"] == lambda_value)
            if summary.get("objective_value") is None:
                continue
            if p3 and not summary.get("feasible", False):
                continue
            options.append((summary["objective_value"], report["origin_ibge_code"], summary))
        if not options:
            if p3 and origin_reports:
                return {"status": "not_found", "global_infeasibility_proven": False}
            return None
        value, origin, summary = min(options, key=lambda item: (item[0], item[1]))
        return {
            "status": summary["status"],
            "origin_ibge_code": origin,
            "objective_value": value,
            "overlap_distance_m": summary["overlap_distance_m"],
            "shared_segment_count": summary["shared_segment_count"],
            "candidate_ids": [route["candidate_id"] for route in summary["routes"]],
        }

    return {
        "scenario": scenario["scenario"],
        "objective": scenario["objective"],
        "vehicle_count": scenario["vehicle_count"],
        "candidate_solution_count": len(solutions),
        "candidate_generation_wall_time_s": round(
            sum(float(solution.get("wall_time_s", 0.0)) for solution in solutions),
            3,
        ),
        "candidate_route_pool_is_complete": False,
        "origins": origin_reports,
        "best_by_policy": {
            "P1": best_summary("p1"),
            "P2": [
                {
                    "lambda": value,
                    **(best_summary("p2", lambda_value=value) or {"status": "not_found"}),
                }
                for value in lambdas
            ],
            "P3": best_summary("p3", p3=True),
        },
    }


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Audita e otimiza a sobreposição P1/P2/P3 sob teto de RAM de 25 GB."
    )
    parser.add_argument("--time-limit-s", type=float, default=30.0)
    parser.add_argument("--variants", type=int, default=8)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=BASE_SEED)
    parser.add_argument("--lambdas", type=float, nargs="+", default=list(DEFAULT_LAMBDAS))
    parser.add_argument("--reference-speed-kph", type=float, default=60.0)
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
        "--mtsp-report",
        type=Path,
        default=ROOT / "data/manifests/mtsp-benchmarks-mt.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data/manifests/overlap-benchmarks-mt.json",
    )
    args = parser.parse_args()
    if not math.isfinite(args.time_limit_s) or args.time_limit_s <= 0:
        parser.error("--time-limit-s deve ser positivo")
    if not 1 <= args.variants <= 32:
        parser.error("--variants deve estar entre 1 e 32")
    if not 1 <= args.workers <= 8:
        parser.error("--workers deve estar entre 1 e 8")
    if not 0 <= args.seed <= 2**31 - 1:
        parser.error("--seed deve estar entre 0 e 2^31-1")
    if (
        not args.lambdas
        or any(not math.isfinite(value) or value < 0 for value in args.lambdas)
        or 0.0 not in args.lambdas
    ):
        parser.error("--lambdas deve conter valores finitos não negativos e incluir zero")
    if not math.isfinite(args.reference_speed_kph) or args.reference_speed_kph <= 0:
        parser.error("--reference-speed-kph deve ser positivo")

    run_started = perf_counter()
    matrix_manifest_path = args.matrix_manifest.resolve()
    graph_manifest_path = args.graph_manifest.resolve()
    mtsp_report_path = args.mtsp_report.resolve()
    output_path = args.output.resolve()
    checkpoint_path = ROOT / "data/processed/d6-overlap-checkpoint.json"
    if output_path.exists():
        raise FileExistsError(f"Relatório D6 já existe; escolha --output: {output_path}")

    matrix_manifest = json.loads(matrix_manifest_path.read_text())
    graph_manifest = json.loads(graph_manifest_path.read_text())
    mtsp_report = json.loads(mtsp_report_path.read_text())
    matrix_path = (ROOT / matrix_manifest["cache_path"]).resolve()
    graph_path = (ROOT / graph_manifest["graph_path"]).resolve()
    matrix_sha256 = _sha256(matrix_path)
    graph_sha256 = _sha256(graph_path)
    mtsp_sha256 = _sha256(mtsp_report_path)
    if matrix_sha256 != matrix_manifest["cache_sha256"]:
        raise ValueError("SHA-256 do cache de matrizes diverge do manifesto")
    if graph_sha256 != graph_manifest["graph_sha256"]:
        raise ValueError("SHA-256 do grafo diverge do manifesto")
    if graph_sha256 != matrix_manifest["graph_sha256"]:
        raise ValueError("Manifestos do grafo e das matrizes divergem")
    if mtsp_report["fingerprint"]["matrix_cache_sha256"] != matrix_sha256:
        raise ValueError("Relatório D5 pertence a outro cache de matrizes")
    if mtsp_report["fingerprint"]["graph_sha256"] != graph_sha256:
        raise ValueError("Relatório D5 pertence a outro grafo")

    scenarios = _scenario_specs(mtsp_report["scenarios"])
    scenario_by_id = {item[0]: item for item in scenarios}
    if len(scenario_by_id) != 8:
        raise ValueError("O relatório D5 deve conter os oito cenários mTSP")
    algorithm_hash = _sha256(ROOT / "backend/app/optimization/visitation/mtsp.py")
    overlap_hash = _sha256(ROOT / "backend/app/optimization/overlap.py")
    fingerprint = {
        "matrix_cache_key": matrix_manifest["cache_key"],
        "matrix_cache_sha256": matrix_sha256,
        "graph_sha256": graph_sha256,
        "mtsp_report_sha256": mtsp_sha256,
        "mtsp_algorithm_version": MTSP_ALGORITHM_VERSION,
        "mtsp_algorithm_sha256": algorithm_hash,
        "overlap_algorithm_sha256": overlap_hash,
        "time_limit_s_per_variant": args.time_limit_s,
        "variants_per_scenario": args.variants,
        "seed": args.seed,
        "lambdas": list(args.lambdas),
        "reference_speed_kph": args.reference_speed_kph,
        "parallel_solver_processes": args.workers,
        "multiprocessing_start_method": "spawn",
    }

    checkpoint_variants: list[dict[str, Any]] = []
    if checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text())
        if checkpoint.get("fingerprint") != fingerprint:
            raise ValueError("Checkpoint D6 pertence a dados ou configuração diferentes")
        checkpoint_variants = checkpoint.get("variants", [])
    resumed_variant_count = len(checkpoint_variants)
    completed = {item["variant_id"] for item in checkpoint_variants}

    pending = []
    task_count = len(scenarios) * max(0, args.variants - 1)
    for scenario_position, scenario in enumerate(scenarios):
        for variant_number in range(1, args.variants):
            seed = args.seed + scenario_position * args.variants + variant_number
            variant_id = f"{scenario[0]}:seed-{seed}"
            if variant_id not in completed:
                pending.append(
                    (
                        scenario_position * max(1, args.variants - 1)
                        + variant_number,
                        scenario_position,
                        scenario,
                        seed,
                    )
                )

    print(
        f"D6: {len(pending)} buscas adicionais, até {args.workers} processos "
        "spawn e teto cgroup de 25 GB.",
        flush=True,
    )
    memory_peak_bytes = [_memory_current_bytes()]
    monitor_stop = threading.Event()
    monitor = threading.Thread(
        target=_sample_memory_peak,
        args=(monitor_stop, memory_peak_bytes),
        name="d6-cgroup-memory-monitor",
        daemon=True,
    )
    monitor.start()
    try:
        with gzip.open(matrix_path, "rt", encoding="utf-8") as source:
            payload = json.load(source)
        if payload.get("cache_key") != matrix_manifest["cache_key"]:
            raise ValueError("Chave do cache diverge do manifesto D3")
        seats = [SeatSnap(**item) for item in payload["municipalities"]]
        if len(seats) != 142:
            raise ValueError("A matriz precisa conter as 142 sedes de Mato Grosso")
        matrices = {
            metric: MetricMatrix(
                metric=metric,
                unit=payload[metric]["unit"],
                costs=payload[metric]["costs"],
                paths=payload[metric]["paths"],
            )
            for metric in ("distance", "time")
        }
        del payload
        worker_matrices = {
            metric: _matrix_without_paths(matrix) for metric, matrix in matrices.items()
        }
        seats_by_code = {seat.ibge_code: seat for seat in seats}
        scenario_results: dict[str, list[dict[str, Any]]] = {
            name: [] for name in scenario_by_id
        }

        for base_scenario in mtsp_report["scenarios"]:
            spec = scenario_by_id[base_scenario["scenario"]]
            base_solution = _restore_d5_solution(
                base_scenario,
                matrices[spec[2]],
                seats_by_code,
            )
            base_solution["candidate_origins_evaluated"] = base_scenario.get(
                "candidate_origins_evaluated", 1
            )
            scenario_results[spec[0]].append(base_solution)

        for item in checkpoint_variants:
            if item["variant_id"] not in completed:
                raise ValueError("Checkpoint D6 contém uma variante incompleta")
            spec = scenario_by_id[item["scenario"]]
            restored_item = {
                **item,
                "routes": [
                    {
                        **route,
                        "legs": [{**leg, "edge_ids": []} for leg in route["legs"]],
                    }
                    for route in item["routes"]
                ],
            }
            _restore_paths(restored_item, matrices[spec[2]], seats_by_code)
            scenario_results[spec[0]].append(restored_item)

        if pending:
            with ProcessPoolExecutor(
                max_workers=args.workers,
                mp_context=multiprocessing.get_context("spawn"),
            ) as pool:
                futures = {
                    pool.submit(
                        _run_variant,
                        position,
                        scenario,
                        seats,
                        worker_matrices[scenario[2]],
                        args.time_limit_s,
                        seed,
                        task_count,
                    ): scenario[0]
                    for position, scenario_position, scenario, seed in pending
                }
                for future in as_completed(futures):
                    item = future.result()
                    spec = scenario_by_id[item["scenario"]]
                    _restore_paths(item, matrices[spec[2]], seats_by_code)
                    scenario_results[spec[0]].append(item)
                    checkpoint_item = {
                        **item,
                        "routes": [
                            {
                                **route,
                                "legs": [
                                    {**leg, "edge_ids": []} for leg in route["legs"]
                                ],
                            }
                            for route in item["routes"]
                        ],
                    }
                    checkpoint_variants = [
                        variant
                        for variant in checkpoint_variants
                        if variant["variant_id"] != item["variant_id"]
                    ]
                    checkpoint_variants.append(checkpoint_item)
                    _write_json(
                        checkpoint_path,
                        {
                            "fingerprint": fingerprint,
                            "variants": checkpoint_variants,
                        },
                    )

        wanted_edge_ids = {
            edge_id
            for scenario_solutions in scenario_results.values()
            for solution in scenario_solutions
            for route in solution["routes"]
            for leg in route["legs"]
            for edge_id in leg["edge_ids"]
        }
        edge_metrics = _read_edge_metrics(graph_path, wanted_edge_ids)
        results = []
        for scenario_id, scenario_solutions in scenario_results.items():
            if len(scenario_solutions) != args.variants:
                raise ValueError(
                    f"{scenario_id}: esperadas {args.variants} soluções "
                    f"candidatas, recebidas {len(scenario_solutions)}"
                )
            base_item = next(
                item for item in mtsp_report["scenarios"] if item["scenario"] == scenario_id
            )
            scenario = {
                "scenario": scenario_id,
                "objective": base_item["objective"],
                "vehicle_count": base_item["vehicle_count"],
            }
            results.append(
                _build_scenario_report(
                    scenario,
                    scenario_solutions,
                    seats,
                    edge_metrics,
                    tuple(args.lambdas),
                    args.reference_speed_kph,
                )
            )

        memory_peak_bytes[0] = max(memory_peak_bytes[0], _memory_current_bytes())
        report = {
            "format_version": 1,
            "dataset": "P1/P2/P3 overlap search for Mato Grosso municipal seats",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "fingerprint": fingerprint,
            "matrix_manifest": str(matrix_manifest_path.relative_to(ROOT)),
            "graph_manifest": str(graph_manifest_path.relative_to(ROOT)),
            "mtsp_report": str(mtsp_report_path.relative_to(ROOT)),
            "city_count": len(seats),
            "reference_speed_kph": args.reference_speed_kph,
            "lambdas": list(args.lambdas),
            "candidate_route_path_method": (
                "D3 shortest paths for each directed visit leg; P2/P3 search the "
                "enumerated route pool and do not claim alternative-road-path completeness"
            ),
            "optimality_claim": (
                "P1/P2 are exact only within the enumerated route-candidate pool; "
                "real P3 not_found is not proof of network infeasibility"
            ),
            "configuration": {
                "time_limit_s_per_variant": args.time_limit_s,
                "variants_per_scenario_including_d5": args.variants,
                "parallel_solver_processes": args.workers,
                "parallel_solver_threads_per_process": 1,
                "multiprocessing_start_method": "spawn",
                "available_logical_cpus": os.cpu_count(),
                "memory_limit_bytes": MEMORY_LIMIT,
                "cgroup_scope_peak_bytes": memory_peak_bytes[0],
                "elapsed_s": round(perf_counter() - run_started, 3),
                "elapsed_scope": "esta invocação; variantes retomadas vêm do checkpoint",
                "resumed_variant_count": resumed_variant_count,
            },
            "scenarios": sorted(results, key=lambda item: item["scenario"]),
        }
        _write_json(output_path, report)
        checkpoint_path.unlink(missing_ok=True)
        print(f"Relatório D6 auditado e gravado: {output_path}", flush=True)
    finally:
        monitor_stop.set()
        monitor.join()


if __name__ == "__main__":
    _main()
