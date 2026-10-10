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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.optimization.visitation.tsp import TSP_ALGORITHM_VERSION, solve_open_tsp
from app.routing.matrices import MetricMatrix, SeatSnap

CUIABA_IBGE_CODE = 5103403
SCENARIOS = (
    ("O1-fixed-cuiaba-distance", "distance", CUIABA_IBGE_CODE),
    ("O1-fixed-cuiaba-time", "time", CUIABA_IBGE_CODE),
    ("O2-variable-origin-distance", "distance", None),
    ("O2-variable-origin-time", "time", None),
)
MAX_MEMORY_BYTES = 25_000_000_000


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


def _memory_limit_bytes() -> int:
    cgroup = Path(Path("/proc/self/cgroup").read_text().strip().split("::")[-1].lstrip("/"))
    limit_path = Path("/sys/fs/cgroup") / cgroup / "memory.max"
    value = limit_path.read_text().strip()
    if not value.isdecimal():
        raise RuntimeError("Runner exige cgroup v2 com teto de memória ativo")
    limit = int(value)
    if limit > MAX_MEMORY_BYTES:
        raise RuntimeError(
            "Runner exige teto <= 25 GB; inicie com systemd-run --user --scope "
            "-p MemoryMax=25000000000"
        )
    return limit


def _main() -> None:
    run_started = perf_counter()
    parser = argparse.ArgumentParser(description="Executa cenários reais Open TSP D4.")
    parser.add_argument("--time-limit-s", type=float, default=300.0)
    parser.add_argument("--verify-existing", action="store_true")
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
    if not math.isfinite(args.time_limit_s) or args.time_limit_s <= 0:
        parser.error("--time-limit-s deve ser positivo")
    memory_limit = _memory_limit_bytes()

    source_path = args.cache_manifest.resolve()
    output_path = args.output.resolve()
    checkpoint_path = ROOT / "data/processed/tsp-d4-checkpoint.json"
    source_manifest = json.loads(source_path.read_text())
    matrix_path = (ROOT / source_manifest["cache_path"]).resolve()
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
            or len(existing_report.get("scenarios", [])) != len(SCENARIOS)
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
    if existing_report is not None:
        existing_report["validation"] = _verify_report(existing_report, seats, matrices)
        _write_json(output_path, existing_report)
        print(f"Relatório validado contra matriz D3: {output_path}", flush=True)
        return
    print(
        f"Matriz pronta: {len(seats)} sedes; RSS pico {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MiB.",
        flush=True,
    )

    for position, (scenario, metric, origin_code) in enumerate(SCENARIOS, start=1):
        if scenario in completed:
            print(f"[{position}/4] checkpoint: {scenario} já concluído.", flush=True)
            continue
        print(
            f"[{position}/4] iniciando {scenario}; limite {args.time_limit_s:g}s.",
            flush=True,
        )
        started = perf_counter()
        solution = solve_open_tsp(
            seats,
            matrices[metric],
            origin_ibge_code=origin_code,
            objective=metric,
            time_limit_s=args.time_limit_s,
            prove_small_instances=False,
        )
        result = {
            "scenario": scenario,
            "origin_policy": "fixed_cuiaba" if origin_code is not None else "optimized_among_all_seats",
            "status": solution.status,
            "method": solution.method,
            "origin_ibge_code": solution.origin_ibge_code,
            "visit_order": list(solution.visit_order),
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
            "candidate_origins_evaluated": solution.candidates_evaluated,
            "solver_status_code": solution.solver_status,
        }
        results.append(result)
        completed.add(scenario)
        _write_json(
            checkpoint_path,
            {"fingerprint": fingerprint, "scenarios": results},
        )
        print(
            f"[{position}/4] {scenario}: {solution.status}, {solution.method}, "
            f"{solution.objective_value:.3f} {solution.unit}, {solution.runtime_s:.1f}s.",
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
            "sequential_scenarios": True,
            "parallel_solver_processes": 1,
            "available_logical_cpus": os.cpu_count(),
            "memory_limit_bytes": memory_limit,
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
) -> dict[str, Any]:
    code_to_index = {seat.ibge_code: index for index, seat in enumerate(seats)}
    expected_codes = set(code_to_index)
    scenarios = {item["scenario"]: item for item in report["scenarios"]}
    checks = []
    for scenario_name, metric, fixed_origin in SCENARIOS:
        scenario = scenarios[scenario_name]
        order = scenario["visit_order"]
        if (
            scenario["status"] != "feasible"
            or len(order) != len(seats)
            or len(set(order)) != len(seats)
            or set(order) != expected_codes
            or scenario["origin_ibge_code"] != order[0]
            or (fixed_origin is not None and order[0] != fixed_origin)
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
                scenario["objective_value"],
                rel_tol=1e-9,
                abs_tol=1e-6,
            )
        ):
            raise ValueError(f"Métrica/caminho diverge da matriz em {scenario_name}")
        for baseline_value in (
            scenario["heuristic_objective_value"],
            scenario["ortools_objective_value"],
        ):
            if baseline_value is not None and scenario["objective_value"] > baseline_value + 1e-6:
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
