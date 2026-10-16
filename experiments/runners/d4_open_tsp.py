from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import resource
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from memory_limit import enforce_memory_limit

MEMORY_LIMIT = enforce_memory_limit()
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.optimization.visitation.tsp import TSP_ALGORITHM_VERSION, solve_open_tsp
from app.routing.matrices import MetricMatrix, SeatSnap
from app.optimization.repetition import REPETITION_ALGORITHM_VERSION
from route_repetition import apply_repetition_rule, load_physical_edges

CUIABA_IBGE_CODE = 5103403
BASE_SEED = 20261008
SCENARIOS = (
    ("O1-fixed-cuiaba-distance", "distance", CUIABA_IBGE_CODE, None),
    ("O1-fixed-cuiaba-time", "time", CUIABA_IBGE_CODE, None),
    ("O2-variable-origin-distance", "distance", None, None),
    ("O2-variable-origin-time", "time", None, None),
)


def _configured_scenarios(args: argparse.Namespace) -> tuple[tuple[str, str, int | None, int | None], ...]:
    if args.objective is None and args.origin_mode is None and args.destination_ibge_code is None:
        return SCENARIOS
    objective = args.objective or "distance"
    origin = args.origin_ibge_code if args.origin_mode == "fixed" else None
    origin_label = f"O1-fixed-{origin}" if origin is not None else "O2-variable-origin"
    destination_label = (
        f"-to-{args.destination_ibge_code}"
        if args.destination_ibge_code is not None
        else ""
    )
    return ((f"{origin_label}{destination_label}-{objective}", objective, origin, args.destination_ibge_code),)


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


def _route_metrics(
    visit_order: tuple[int, ...],
    seats: list[SeatSnap],
    matrices: dict[str, MetricMatrix],
) -> tuple[float, float]:
    code_to_index = {seat.ibge_code: index for index, seat in enumerate(seats)}
    distance_m = 0.0
    travel_time_s = 0.0
    for from_code, to_code in zip(visit_order, visit_order[1:], strict=False):
        from_index = code_to_index[from_code]
        to_index = code_to_index[to_code]
        distance = matrices["distance"].costs[from_index][to_index]
        travel_time = matrices["time"].costs[from_index][to_index]
        if distance is None or travel_time is None:
            raise ValueError("Rota D4 contém uma perna sem custo dirigido no cache D3")
        distance_m += distance
        travel_time_s += travel_time
    return distance_m, travel_time_s


def _main() -> None:
    run_started = perf_counter()
    parser = argparse.ArgumentParser(description="Executa cenários reais Open TSP D4.")
    parser.add_argument("--time-limit-s", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=BASE_SEED)
    parser.add_argument("--verify-existing", action="store_true")
    parser.add_argument("--objective", choices=("distance", "time"))
    parser.add_argument("--origin-mode", choices=("auto", "fixed"))
    parser.add_argument("--origin-ibge-code", type=int)
    parser.add_argument("--destination-ibge-code", type=int)
    parser.add_argument("--preview-path", type=Path)
    parser.add_argument("--repetition-policy", choices=("allow", "reduce", "forbid"), default="allow")
    parser.add_argument("--graph-manifest", type=Path, default=ROOT / "data/manifests/road-network-mt.json")
    parser.add_argument(
        "--cache-manifest",
        type=Path,
        default=ROOT / "data/manifests/travel-matrix-mt.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data/manifests/tsp-benchmarks-mt.json",
    )
    args = parser.parse_args()
    scenarios = _configured_scenarios(args)
    if args.origin_mode == "fixed" and args.origin_ibge_code is None:
        parser.error("--origin-mode fixed exige --origin-ibge-code")
    if args.origin_mode != "fixed" and args.origin_ibge_code is not None:
        parser.error("--origin-ibge-code só pode ser usado com --origin-mode fixed")
    if (
        args.destination_ibge_code is not None
        and args.origin_ibge_code == args.destination_ibge_code
    ):
        parser.error("origem e destino precisam ser diferentes")
    if not math.isfinite(args.time_limit_s) or args.time_limit_s <= 0:
        parser.error("--time-limit-s deve ser positivo")
    if not 0 <= args.seed <= 2**31 - len(scenarios):
        parser.error("--seed deve permitir seeds de cenário entre 0 e 2^31-1")
    source_path = args.cache_manifest.resolve()
    output_path = args.output.resolve()
    checkpoint_path = output_path.with_suffix(".checkpoint.json")
    source_manifest = json.loads(source_path.read_text())
    matrix_path = (ROOT / source_manifest["cache_path"]).resolve()
    graph_manifest = json.loads(args.graph_manifest.read_text())
    graph_path = ROOT / graph_manifest["graph_path"]
    if graph_manifest["graph_sha256"] != source_manifest["graph_sha256"] or _sha256(graph_path) != graph_manifest["graph_sha256"]:
        raise ValueError("Grafo físico diverge da matriz D3")
    matrix_sha256 = _sha256(matrix_path)
    if matrix_sha256 != source_manifest["cache_sha256"]:
        raise ValueError("SHA-256 do cache D3 diverge do manifesto")
    source_code_sha256 = _sha256(
        ROOT / "backend/app/optimization/visitation/tsp.py"
    )
    fingerprint = {
        "matrix_cache_key": source_manifest["cache_key"],
        "matrix_cache_sha256": matrix_sha256,
        "algorithm_version": TSP_ALGORITHM_VERSION,
        "algorithm_source_sha256": source_code_sha256,
        "time_limit_s_per_scenario": args.time_limit_s,
        "seed": args.seed,
        "scenario_ids": [item[0] for item in scenarios],
        "repetition_policy": args.repetition_policy,
        "repetition_algorithm_version": REPETITION_ALGORITHM_VERSION,
        "repetition_algorithm_sha256": _sha256(ROOT / "backend/app/optimization/repetition.py"),
        "repetition_runner_sha256": _sha256(Path(__file__).with_name("route_repetition.py")),
    }

    existing_report = None
    if output_path.is_file():
        if not args.verify_existing:
            raise FileExistsError(
                f"Relatório existente; use --verify-existing: {output_path}"
            )
        existing_report = json.loads(output_path.read_text())
        if (
            existing_report.get("fingerprint") != fingerprint
            or len(existing_report.get("scenarios", [])) != len(scenarios)
        ):
            raise ValueError("Relatório existente diverge dos dados ou código atuais")

    results: list[dict[str, Any]] = []
    if checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text())
        if checkpoint.get("fingerprint") != fingerprint:
            raise ValueError("Checkpoint D4 pertence a dados ou código diferentes")
        results = checkpoint.get("scenarios", [])
    completed = {item["scenario"] for item in results}

    print(f"Cache SHA-256 validado; tamanho {matrix_path.stat().st_size:,} bytes.", flush=True)
    print("Carregando matriz; execução sequencial, sem processos paralelos.", flush=True)
    with gzip.open(matrix_path, "rt", encoding="utf-8") as source:
        payload = json.load(source)
    if payload.get("cache_key") != source_manifest["cache_key"]:
        raise ValueError("Chave do cache diverge do manifesto")
    seats = [SeatSnap(**item) for item in payload["municipalities"]]
    if len(seats) != source_manifest["municipality_count"]:
        raise ValueError("Quantidade de sedes diverge do manifesto D3")
    matrices = {
        metric: MetricMatrix(
            metric=metric,
            unit=payload[metric]["unit"],
            costs=payload[metric]["costs"],
            paths=payload[metric]["paths"],
        )
        for metric in ("distance", "time")
    }
    if existing_report is not None:
        existing_report["validation"] = _verify_report(existing_report, seats, matrices, scenarios)
        _write_json(output_path, existing_report)
        print(f"Relatório validado contra matriz D3: {output_path}", flush=True)
        return
    repetition_edges = load_physical_edges(graph_path, matrices=matrices.values()) if args.repetition_policy != "allow" else None
    print(
        f"Matriz pronta: {len(seats)} sedes; RSS pico {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MiB.",
        flush=True,
    )

    for position, (scenario, metric, origin_code, destination_code) in enumerate(scenarios, start=1):
        if scenario in completed:
            print(f"[{position}/{len(scenarios)}] checkpoint: {scenario} já concluído.", flush=True)
            continue
        print(
            f"[{position}/{len(scenarios)}] iniciando {scenario}; limite {args.time_limit_s:g}s.",
            flush=True,
        )
        started = perf_counter()
        scenario_seed = args.seed + position - 1
        last_preview_at = 0.0

        def publish_candidate(
            order: tuple[int, ...], value: float, *, force: bool = False
        ) -> None:
            nonlocal last_preview_at
            if args.preview_path is not None:
                now = perf_counter()
                if not force and now - last_preview_at < 0.5:
                    return
                _write_json(
                    args.preview_path,
                    {
                        "scenario": scenario,
                        "objective_value": value,
                        "candidate_count": 1,
                        "routes": [{"agent_id": 1, "visit_order": list(order)}],
                    },
                )
                last_preview_at = now

        solution = solve_open_tsp(
            seats,
            matrices[metric],
            origin_ibge_code=origin_code,
            destination_ibge_code=destination_code,
            objective=metric,
            time_limit_s=args.time_limit_s * (0.75 if args.repetition_policy != "allow" else 1),
            prove_small_instances=False,
            seed=scenario_seed,
            progress_callback=publish_candidate,
        )
        if solution.objective_value is not None:
            publish_candidate(solution.visit_order, solution.objective_value, force=True)
        distance_total_m, completion_time_s = _route_metrics(
            solution.visit_order,
            seats,
            matrices,
        )
        result = {
            "scenario": scenario,
            "seed": scenario_seed,
            "origin_policy": (
                "fixed_cuiaba" if origin_code == CUIABA_IBGE_CODE
                else "fixed" if origin_code is not None
                else "optimized_among_all_seats"
            ),
            "status": solution.status,
            "method": solution.method,
            "origin_ibge_code": solution.origin_ibge_code,
            "destination_ibge_code": destination_code,
            "visit_order": list(solution.visit_order),
            "legs": [
                {
                    "from_ibge_code": leg.from_ibge_code,
                    "to_ibge_code": leg.to_ibge_code,
                    "edge_ids": list(leg.edge_ids),
                }
                for leg in solution.legs
            ],
            "objective": solution.objective,
            "unit": solution.unit,
            "objective_value": solution.objective_value,
            "travel_cost": solution.travel_cost,
            "total_service_time_s": solution.total_service_time_s,
            "heuristic_objective_value": solution.heuristic_objective_value,
            "ortools_objective_value": solution.ortools_objective_value,
            "leg_count": len(solution.legs),
            "directed_edge_count": sum(len(leg.edge_ids) for leg in solution.legs),
            "runtime_s": solution.runtime_s,
            "wall_time_s": perf_counter() - started,
            "distance_total_m": distance_total_m,
            "total_distance_m": distance_total_m,
            "distance_by_agent_m": [distance_total_m],
            "max_agent_distance_m": distance_total_m,
            "completion_time_s": completion_time_s,
            "completion_time_by_agent_s": [completion_time_s],
            "time_max_s": completion_time_s,
            "municipalities_served": len(set(solution.visit_order)),
            "municipalities_by_agent": [len(set(solution.visit_order))],
            "overlap_distance_m": 0.0,
            "candidate_origins_evaluated": solution.candidates_evaluated,
            "solver_status_code": solution.solver_status,
        }
        if result["visit_order"]:
            physical_edges = repetition_edges if repetition_edges is not None else load_physical_edges(
                graph_path, selected_edge_ids=(edge_id for leg in result["legs"] for edge_id in leg["edge_ids"]),
            )
            apply_repetition_rule(result, seats, matrices[metric], physical_edges, policy=args.repetition_policy,
                                  time_limit_s=max(0.001, args.time_limit_s * 0.25), seed=scenario_seed,
                                  preview_path=args.preview_path)
        results.append(result)
        completed.add(scenario)
        _write_json(
            checkpoint_path,
            {"fingerprint": fingerprint, "scenarios": results},
        )
        print(
            f"[{position}/{len(scenarios)}] {scenario}: {result['status']}, {result['method']}, "
            f"objetivo {result['objective_value']} {solution.unit}, {result['runtime_s']:.1f}s.",
            flush=True,
        )

    report = {
        "format_version": 1,
        "dataset": "Open TSP P1 / D4 for Mato Grosso municipal seats",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "fingerprint": fingerprint,
        "matrix_manifest": str(source_path.relative_to(ROOT)),
        "city_count": len(seats),
        "algorithm": "OR-Tools RoutingModel with nearest-neighbor baseline and artificial open end",
        "configuration": {
            "time_limit_s_per_scenario": args.time_limit_s,
            "seed": args.seed,
            "scenario_count": len(scenarios),
            "sequential_scenarios": True,
            "parallel_solver_processes": 1,
            "available_logical_cpus": os.cpu_count(),
            "memory_limit_bytes": MEMORY_LIMIT,
            "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
            "elapsed_s": round(perf_counter() - run_started, 3),
        },
        "optimality_claim": "feasible best-known only; no global proof for 142 cities",
        "scenarios": sorted(results, key=lambda item: item["scenario"]),
    }
    _write_json(output_path, report)
    checkpoint_path.unlink(missing_ok=True)
    print(f"Relatório gravado: {output_path}", flush=True)


def _verify_report(
    report: dict[str, Any],
    seats: list[SeatSnap],
    matrices: dict[str, MetricMatrix],
    scenarios_to_check: tuple[tuple[str, str, int | None, int | None], ...] = SCENARIOS,
) -> dict[str, Any]:
    code_to_index = {seat.ibge_code: index for index, seat in enumerate(seats)}
    expected_codes = set(code_to_index)
    scenarios = {item["scenario"]: item for item in report["scenarios"]}
    checks = []
    for scenario_name, metric, fixed_origin, fixed_destination in scenarios_to_check:
        scenario = scenarios[scenario_name]
        order = scenario["visit_order"]
        if scenario["status"] == "not_found" and scenario.get("repetition_policy") == "forbid" and not order:
            checks.append({"scenario": scenario_name, "no_violating_route_returned": True})
            continue
        if (
            scenario["status"] != "feasible"
            or len(order) != len(seats)
            or len(set(order)) != len(seats)
            or set(order) != expected_codes
            or scenario["origin_ibge_code"] != order[0]
            or (fixed_origin is not None and order[0] != fixed_origin)
            or (fixed_destination is not None and order[-1] != fixed_destination)
            or len(order) < 2
            or order[-1] == order[0]
        ):
            raise ValueError(f"Cobertura/origem inválida em {scenario_name}")

        matrix = matrices[metric]
        travel_cost = 0.0
        directed_edge_count = 0
        for from_code, to_code in zip(order, order[1:]):
            from_index = code_to_index[from_code]
            to_index = code_to_index[to_code]
            cost = matrix.costs[from_index][to_index]
            path = matrix.paths[from_index][to_index]
            if cost is None or path is None:
                raise ValueError(f"Trecho dirigido ausente em {scenario_name}")
            travel_cost += cost
            directed_edge_count += len(path)
        if (
            len(order) - 1 != scenario["leg_count"]
            or directed_edge_count != scenario["directed_edge_count"]
            or not math.isclose(travel_cost, scenario["travel_cost"], rel_tol=1e-9, abs_tol=1e-6)
            or not math.isclose(
                travel_cost,
                scenario.get("base_objective_value", scenario["objective_value"]),
                rel_tol=1e-9,
                abs_tol=1e-6,
            )
        ):
            raise ValueError(f"Métrica/caminho diverge da matriz em {scenario_name}")
        for baseline_value in (
            scenario["heuristic_objective_value"],
            scenario["ortools_objective_value"],
        ):
            if scenario.get("repetition_policy", "allow") == "allow" and baseline_value is not None and scenario["objective_value"] > baseline_value + 1e-6:
                raise ValueError(f"Solução final pior que uma candidata em {scenario_name}")
        checks.append(
            {
                "scenario": scenario_name,
                "all_cities_once": True,
                "open_route": True,
                "directed_matrix_cost_matches": True,
                "directed_edge_count": directed_edge_count,
            }
        )
    return {
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "city_count": len(seats),
        "all_scenarios_verified": True,
        "checks": checks,
    }


if __name__ == "__main__":
    _main()
