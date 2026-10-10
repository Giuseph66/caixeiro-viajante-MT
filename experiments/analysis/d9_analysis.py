"""Produce reproducible descriptive statistics from archived D9 runs."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "experiments/configurations/d9-mt.json"
DEFAULT_RESULTS = ROOT / "experiments/results/d9-mato-grosso-2026-10-10"
DEFAULT_OUTPUT = ROOT / "experiments/analysis/d9-mato-grosso-2026-10-10"
STAGE_FILES = {"d4": "d4-open-tsp.json.gz", "d5": "d5-open-mtsp.json.gz", "d6": "d6-overlap.json.gz"}
NUMERIC_FIELDS = (
    "objective_value",
    "distance_total_m",
    "max_agent_distance_m",
    "time_max_s",
    "overlap_distance_m",
    "overlap_percent",
    "algorithm_runtime_s",
    "wall_time_s",
)


def _read_gzip_json(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as source:
        return json.load(source)


def _selected_policy(
    scenario_report: dict[str, Any],
    policy: str,
    lambda_value: float | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    selected = scenario_report.get("best_by_policy", {}).get(policy)
    if policy == "P2":
        selected = next(
            (
                item
                for item in selected or []
                if lambda_value is not None
                and math.isclose(float(item.get("lambda", -1)), lambda_value)
            ),
            None,
        )
    selected = selected or {"status": "not_found"}
    origin_code = selected.get("origin_ibge_code")
    if origin_code is None:
        return selected, None
    origin = next(
        (
            item
            for item in scenario_report.get("origins", [])
            if item.get("origin_ibge_code") == origin_code
        ),
        None,
    )
    if origin is None:
        raise ValueError(f"Origem selecionada {origin_code} ausente do relatório D6")
    details: Any = origin[policy.lower()]
    if policy == "P2":
        details = next(
            item
            for item in details
            if math.isclose(float(item.get("lambda", -1)), float(lambda_value))
        )
    return selected, details


def _route_metrics(details: dict[str, Any] | None) -> dict[str, Any]:
    if details is None:
        return {
            "distance_total_m": None,
            "distance_by_agent_m": [],
            "max_agent_distance_m": None,
            "time_by_agent_s": [],
            "time_max_s": None,
            "municipalities_by_agent": [],
            "municipalities_served": 0,
            "overlap_distance_m": None,
            "overlap_percent": None,
            "objective_value": None,
            "base_objective": None,
            "proof_scope": None,
        }
    routes = details.get("routes", [])
    distances = [float(route["distance_m"]) for route in routes]
    times = [float(route["completion_time_s"]) for route in routes]
    city_counts = [int(route["served_city_count"]) for route in routes]
    origin_assignment_agent_id = None
    if routes:
        origin_index = min(
            range(len(routes)), key=lambda index: int(routes[index]["agent_id"])
        )
        city_counts[origin_index] += 1
        origin_assignment_agent_id = int(routes[origin_index]["agent_id"])
    served_city_codes = {
        int(code)
        for route in routes
        for code in route.get("visit_order", [])
    }
    total_distance = sum(distances) if routes else None
    overlap = details.get("overlap_distance_m")
    return {
        "distance_total_m": total_distance,
        "distance_by_agent_m": distances,
        "max_agent_distance_m": max(distances) if distances else None,
        "time_by_agent_s": times,
        "time_max_s": max(times) if times else None,
        "municipalities_by_agent": city_counts,
        "municipalities_served": len(served_city_codes),
        "origin_assignment_agent_id": origin_assignment_agent_id,
        "overlap_distance_m": overlap,
        "overlap_percent": (
            float(overlap) * 100 / total_distance
            if overlap is not None and total_distance
            else None
        ),
        "objective_value": details.get("objective_value"),
        "base_objective": details.get("base_objective"),
        "proof_scope": details.get("proof_scope"),
    }


def _d6_observation(
    scenario_config: dict[str, Any],
    scenario_report: dict[str, Any],
    repetition: int,
    seeds: dict[str, int],
    lambda_value: float | None,
) -> dict[str, Any]:
    policy = scenario_config["policy"]
    selected, details = _selected_policy(scenario_report, policy, lambda_value)
    metrics = _route_metrics(details)
    return {
        "experiment_id": scenario_config["id"],
        "repetition": repetition,
        "seed_d5": seeds["d5"],
        "seed_d6": seeds["d6"],
        "agents": scenario_config["agents"],
        "origin_mode": scenario_config["origin"],
        "origin_ibge_code": selected.get("origin_ibge_code"),
        "policy": policy,
        "objective": scenario_config["objective"],
        "lambda": lambda_value,
        "status": selected.get("status", "not_found"),
        "optimality_scope": metrics["proof_scope"] or (
            "no feasible candidate found; no global infeasibility proof"
            if selected.get("status") == "not_found"
            else "best-known within partial route candidate pool"
        ),
        **metrics,
        "algorithm_runtime_s": scenario_report.get("candidate_generation_wall_time_s"),
        "candidate_generation_wall_time_s": scenario_report.get(
            "candidate_generation_wall_time_s"
        ),
        "d6_batch_elapsed_s": None,
        "candidate_route_pool_is_complete": scenario_report.get(
            "candidate_route_pool_is_complete", False
        ),
    }


def _collect(
    config: dict[str, Any],
    results_dir: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    index_path = results_dir / "index.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"Manifesto de execuções D9 ausente: {index_path}")
    index = json.loads(index_path.read_text())
    observations: list[dict[str, Any]] = []
    lambda_observations: list[dict[str, Any]] = []
    d5_observations: list[dict[str, Any]] = []
    completed_repetitions = 0
    for repeat_record in index.get("repetitions", []):
        if repeat_record.get("status") != "complete":
            continue
        repetition = int(repeat_record["repetition"])
        seeds = repeat_record["seeds"]
        repeat_dir = results_dir / f"repeat-{repetition:02d}"
        d4 = _read_gzip_json(repeat_dir / STAGE_FILES["d4"])
        d5 = _read_gzip_json(repeat_dir / STAGE_FILES["d5"])
        d6 = _read_gzip_json(repeat_dir / STAGE_FILES["d6"])
        d4_by_id = {item["scenario"]: item for item in d4["scenarios"]}
        d6_by_id = {item["scenario"]: item for item in d6["scenarios"]}
        completed_repetitions += 1

        for result in d5["scenarios"]:
            d5_observations.append(
                {
                    "scenario": result["scenario"],
                    "repetition": repetition,
                    "seed": result["seed"],
                    "status": result["status"],
                    "optimality_scope": "feasible best-known; no global proof for 142 cities",
                    "objective": result["objective"],
                    "objective_value": result["objective_value"],
                    "origin_ibge_code": result["origin_ibge_code"],
                    "runtime_s": result["runtime_s"],
                    "wall_time_s": result["wall_time_s"],
                    "total_distance_m": result["total_distance_m"],
                    "time_max_s": result["time_max_s"],
                    "candidate_origins_evaluated": result[
                        "candidate_origins_evaluated"
                    ],
                }
            )

        for item in config["scenarios"]:
            if item["agents"] == 1:
                result = d4_by_id[item["d4_scenario"]]
                row = {
                    "experiment_id": item["id"],
                    "repetition": repetition,
                    "seed_d4": result["seed"],
                    "seed_d5": None,
                    "seed_d6": None,
                    "agents": 1,
                    "origin_mode": item["origin"],
                    "origin_ibge_code": result["origin_ibge_code"],
                    "policy": "P1",
                    "objective": item["objective"],
                    "lambda": None,
                    "status": result["status"],
                    "optimality_scope": "feasible best-known; no global proof for 142 cities",
                    "objective_value": result["objective_value"],
                    "base_objective": result["objective_value"],
                    "distance_total_m": result["distance_total_m"],
                    "distance_by_agent_m": result["distance_by_agent_m"],
                    "max_agent_distance_m": result["max_agent_distance_m"],
                    "time_by_agent_s": result["completion_time_by_agent_s"],
                    "time_max_s": result["time_max_s"],
                    "municipalities_by_agent": result["municipalities_by_agent"],
                    "municipalities_served": result["municipalities_served"],
                    "overlap_distance_m": 0.0,
                    "overlap_percent": 0.0,
                    "algorithm_runtime_s": result["runtime_s"],
                    "wall_time_s": result["wall_time_s"],
                    "candidate_generation_wall_time_s": None,
                    "d6_batch_elapsed_s": None,
                    "candidate_route_pool_is_complete": True,
                }
                observations.append(row)
                continue

            d6_scenario = d6_by_id[item["d6_scenario"]]
            lambda_value = float(config["lambda_main"]) if item["policy"] == "P2" else None
            row = _d6_observation(item, d6_scenario, repetition, seeds, lambda_value)
            row["seed_d4"] = None
            row["d6_batch_elapsed_s"] = d6["configuration"]["elapsed_s"]
            observations.append(row)

        for item in config["scenarios"]:
            if item["policy"] != "P2":
                continue
            d6_scenario = d6_by_id[item["d6_scenario"]]
            for lambda_value in sorted(
                set(float(value) for value in [*config["lambda_sensitivity"], config["lambda_main"]])
            ):
                row = _d6_observation(
                    item,
                    d6_scenario,
                    repetition,
                    seeds,
                    lambda_value,
                )
                row["seed_d4"] = None
                row["d6_batch_elapsed_s"] = d6["configuration"]["elapsed_s"]
                lambda_observations.append(row)

    metadata = {
        "study_id": index.get("study_id"),
        "index_status": index.get("status"),
        "repetitions_expected": config["repetitions"],
        "repetitions_completed": completed_repetitions,
        "input_hashes": index.get("input_hashes"),
        "git_revision": index.get("git_revision"),
        "runtime_environment": index.get("runtime_environment"),
        "source_snapshot": (
            json.loads((results_dir / "source-snapshot.json").read_text())
            if (results_dir / "source-snapshot.json").is_file()
            else None
        ),
    }
    return observations, lambda_observations, d5_observations, metadata


def _summarize(
    observations: list[dict[str, Any]],
    *,
    by_lambda: bool = False,
) -> list[dict[str, Any]]:
    summary = []
    grouped: dict[tuple[str, float | None], list[dict[str, Any]]] = {}
    for row in observations:
        group_key = (
            row["experiment_id"],
            float(row["lambda"]) if by_lambda and row.get("lambda") is not None else None,
        )
        grouped.setdefault(group_key, []).append(row)
    for (experiment_id, lambda_value), rows in sorted(grouped.items()):
        feasible = [row for row in rows if row["status"] == "feasible"]
        status_counts: dict[str, int] = {}
        for row in rows:
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        best_objective = min(
            (float(row["objective_value"]) for row in feasible if row["objective_value"] is not None),
            default=None,
        )
        item: dict[str, Any] = {
            "experiment_id": experiment_id,
            "agents": rows[0]["agents"],
            "origin_mode": rows[0]["origin_mode"],
            "policy": rows[0]["policy"],
            "objective": rows[0]["objective"],
            "lambda": lambda_value if by_lambda else rows[0]["lambda"],
            "repetitions": len(rows),
            "feasible_repetitions": len(feasible),
            "status_counts": status_counts,
            "best_observed_objective": best_objective,
        }
        for field in NUMERIC_FIELDS:
            values = [float(row[field]) for row in feasible if row.get(field) is not None]
            item[f"{field}_mean"] = mean(values) if values else None
            item[f"{field}_median"] = median(values) if values else None
            item[f"{field}_sample_sd"] = stdev(values) if len(values) > 1 else None
            item[f"{field}_min"] = min(values) if values else None
            item[f"{field}_max"] = max(values) if values else None
        gaps = [
            (float(row["objective_value"]) - best_objective) * 100 / best_objective
            for row in feasible
            if row.get("objective_value") is not None and best_objective not in (None, 0)
        ]
        item["objective_gap_to_best_observed_median_pct"] = median(gaps) if gaps else None
        summary.append(item)
    return summary


def _summarize_d5(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in observations:
        grouped.setdefault(row["scenario"], []).append(row)
    metrics = (
        "runtime_s",
        "wall_time_s",
        "objective_value",
        "total_distance_m",
        "time_max_s",
    )
    for scenario, rows in sorted(grouped.items()):
        feasible = [
            row
            for row in rows
            if row["status"] in {"feasible", "optimal_proven"}
        ]
        status_counts: dict[str, int] = {}
        for row in rows:
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        item: dict[str, Any] = {
            "scenario": scenario,
            "objective": rows[0]["objective"],
            "repetitions": len(rows),
            "feasible_repetitions": len(feasible),
            "status_counts": status_counts,
        }
        for field in metrics:
            values = [
                float(row[field])
                for row in feasible
                if row.get(field) is not None
            ]
            item[f"{field}_mean"] = mean(values) if values else None
            item[f"{field}_median"] = median(values) if values else None
            item[f"{field}_sample_sd"] = stdev(values) if len(values) > 1 else None
            item[f"{field}_min"] = min(values) if values else None
            item[f"{field}_max"] = max(values) if values else None
        summary.append(item)
    return summary


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as target:
        fieldnames = list(dict.fromkeys(key for row in rows for key in row))
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (list, dict))
                    else value
                    for key, value in row.items()
                }
            )


def _report_markdown(
    metadata: dict[str, Any],
    summary: list[dict[str, Any]],
    d5_summary: list[dict[str, Any]],
    lambda_summary: list[dict[str, Any]],
) -> str:
    lines = [
        "# Relatório experimental D9 — Mato Grosso",
        "",
        f"Estado do lote: **{metadata['index_status']}**; repetições concluídas: **{metadata['repetitions_completed']}/{metadata['repetitions_expected']}**.",
        "",
        "Os resultados usam os hashes congelados no manifesto do lote. D4 é best-known em 142 sedes. P1/P2 são exatos somente no conjunto parcial de rotas candidatas; P3 `not_found` não prova inviabilidade da rede.",
        "",
    ]
    source_snapshot = metadata.get("source_snapshot")
    if source_snapshot:
        lines.extend(
            [
                f"Código do cálculo: `{source_snapshot['calculation_code_commit']}`; "
                f"análise: `{source_snapshot['analysis_code_commit']}`. "
                f"HEAD registrado pelo runner: `{source_snapshot['index_git_revision']}`.",
                "",
            ]
        )
    lines.extend(
        [
            "## Estatísticas por configuração",
            "",
            "| ID | k | Origem | Política | Objetivo | n viável | Status | Busca mediana (s) | D total mediana (km) | T máximo mediano (h) | Sobreposição mediana (km) | Gap mediano vs. melhor observado |",
            "|---|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in summary:
        distance = row["distance_total_m_median"]
        time_max = row["time_max_s_median"]
        overlap = row["overlap_distance_m_median"]
        runtime = row["algorithm_runtime_s_median"]
        gap = row["objective_gap_to_best_observed_median_pct"]
        lines.append(
            "| {id} | {k} | {origin} | {policy} | {objective} | {n} | {status} | {runtime} | {distance} | {time} | {overlap} | {gap} |".format(
                id=row["experiment_id"],
                k=row["agents"],
                origin=row["origin_mode"],
                policy=row["policy"],
                objective=row["objective"],
                n=row["feasible_repetitions"],
                status=json.dumps(row["status_counts"], ensure_ascii=False),
                runtime=f"{runtime:.1f}" if runtime is not None else "—",
                distance=f"{distance / 1000:.1f}" if distance is not None else "—",
                time=f"{time_max / 3600:.2f}" if time_max is not None else "—",
                overlap=f"{overlap / 1000:.1f}" if overlap is not None else "—",
                gap=f"{gap:.2f}%" if gap is not None else "—",
            )
        )
    lines.extend(
        [
            "",
            "## Medições do D5",
            "",
            "D5 é resumido separadamente antes da seleção de políticas D6; os tempos abaixo não são filtrados pelo status da rota escolhida posteriormente.",
            "",
            "| Cenário | n viável | Status | Solver mediano (s) | Parede mediana (s) | D total mediana (km) | T máximo mediano (h) |",
            "|---|---:|---|---:|---:|---:|---:|",
        ]
    )
    for row in d5_summary:
        runtime = row["runtime_s_median"]
        wall = row["wall_time_s_median"]
        distance = row["total_distance_m_median"]
        time_max = row["time_max_s_median"]
        runtime_text = f"{runtime:.1f}" if runtime is not None else "—"
        wall_text = f"{wall:.1f}" if wall is not None else "—"
        distance_text = f"{distance / 1000:.1f}" if distance is not None else "—"
        time_text = f"{time_max / 3600:.2f}" if time_max is not None else "—"
        lines.append(
            f"| {row['scenario']} | {row['feasible_repetitions']} | "
            f"{json.dumps(row['status_counts'], ensure_ascii=False)} | "
            f"{runtime_text} | {wall_text} | {distance_text} | {time_text} |"
        )
    lines.extend(
        [
            "",
            "## Sensibilidade de P2",
            "",
            "Os valores λ=0, 0,25, 0,5, 1 e 2 foram selecionados sobre os mesmos candidatos de cada repetição. λ=0 é conferido contra P1 pelo runner D6.",
            "",
            "| ID | λ | n viável | Objetivo mediano | Sobreposição mediana (km) | T máximo mediano (h) |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in lambda_summary:
        objective = row["objective_value_median"]
        overlap = row["overlap_distance_m_median"]
        time_max = row["time_max_s_median"]
        objective_text = f"{objective:.3f}" if objective is not None else "—"
        overlap_text = f"{overlap / 1000:.1f}" if overlap is not None else "—"
        time_text = f"{time_max / 3600:.2f}" if time_max is not None else "—"
        lines.append(
            f"| {row['experiment_id']} | {row['lambda']} | "
            f"{row['feasible_repetitions']} | {objective_text} | "
            f"{overlap_text} | {time_text} |"
        )
    lines.extend(
        [
            "",
            "## Arquivos reproduzíveis",
            "",
            "- `observations.csv`: métricas por repetição.",
            "- `d5-observations.csv`: métricas D5 por cenário e repetição.",
            "- `lambda-sensitivity.csv`: resultados de P2 por λ e repetição.",
            "- `summary.json`: estatísticas descritivas e metadados.",
            "- `experiments/results/.../index.json`: seeds, hashes, estado e artefatos de cada repetição.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analisa estatisticamente os resultados D9.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    observations, lambda_observations, d5_observations, metadata = _collect(
        config,
        args.results_dir.resolve(),
    )
    summary = _summarize(observations)
    d5_summary = _summarize_d5(d5_observations)
    lambda_summary = _summarize(lambda_observations, by_lambda=True)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "observations.csv", observations)
    _write_csv(output_dir / "d5-observations.csv", d5_observations)
    _write_csv(output_dir / "lambda-sensitivity.csv", lambda_observations)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "metadata": metadata,
                "config": config,
                "scenario_summary": summary,
                "d5_scenario_summary": d5_summary,
                "lambda_sensitivity_summary": lambda_summary,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(
        _report_markdown(metadata, summary, d5_summary, lambda_summary),
        encoding="utf-8",
    )
    print(f"Análise D9 gravada em {output_dir}")


if __name__ == "__main__":
    main()
