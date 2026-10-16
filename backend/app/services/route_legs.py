"""Recupera a geometria viária de resultados antigos que não gravaram `legs`.

Resultados de D4/D5 gerados antes da gravação de arestas só têm `visit_order`.
Como a matriz de viagem em cache guarda o caminho de cada par de sedes, os
trechos são reconstruídos a partir dela — exatamente os caminhos que o solver
usou — e gravados num arquivo auxiliar ao lado do resultado, sem alterar o JSON
original (que carrega fingerprint e proveniência).

Uso: python -m app.services.route_legs [--run-id UUID ...] [--force]
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from pathlib import Path
from typing import Any

from app.core.config import PROJECT_ROOT

FORMAT_VERSION = 1
COST_TOLERANCE = 1e-6


def legs_sidecar_path(result_path: Path) -> Path:
    return result_path.with_suffix(".legs.json.gz")


def load_legs_sidecar(result_path: Path) -> dict[tuple[str, tuple[int, ...]], list[dict[str, Any]]]:
    """Mapeia (cenário, visit_order) para a lista de legs com `edge_ids`."""
    path = legs_sidecar_path(result_path)
    if not path.is_file():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as source:
        payload = json.load(source)
    if payload.get("format_version") != FORMAT_VERSION:
        return {}
    return {
        (route["scenario"], tuple(route["visit_order"])): [
            {"edge_ids": edge_ids} for edge_ids in route["leg_edge_ids"]
        ]
        for route in payload.get("routes", [])
    }


def attach_sidecar_legs(
    result_path: Path, scenario: str, routes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Completa rotas sem `legs` com a geometria do arquivo auxiliar, se existir."""
    missing = [route for route in routes if not route.get("legs") and route.get("visit_order")]
    if not missing:
        return routes
    sidecar = load_legs_sidecar(result_path)
    if not sidecar:
        return routes
    for route in missing:
        legs = sidecar.get((scenario, tuple(int(code) for code in route["visit_order"])))
        if legs:
            route["legs"] = legs
            route.pop("geometry_approximate", None)
    return routes


def _scenario_routes(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Rotas de um cenário como {visit_order, expected_cost}; vazio se não aplicável."""
    if "origins" in scenario or scenario.get("status") not in (None, "feasible"):
        return []
    if "routes" in scenario:
        metric = scenario.get("objective")
        routes = []
        for route in scenario["routes"]:
            if route.get("legs") or not route.get("visit_order"):
                continue
            cost_key = "distance_m" if metric == "distance" else "road_time_s"
            routes.append({"visit_order": route["visit_order"], "expected_cost": route.get(cost_key)})
        return routes
    if scenario.get("visit_order") and not scenario.get("legs"):
        return [{"visit_order": scenario["visit_order"], "expected_cost": scenario.get("travel_cost")}]
    return []


def _load_matrix(project_root: Path) -> tuple[dict[int, int], dict[str, list[list[Any]]], dict[str, list[list[Any]]], str]:
    manifest = json.loads(
        (project_root / "data/manifests/travel-matrix-mt.json").read_text(encoding="utf-8")
    )
    cache_path = project_root / manifest["cache_path"]
    print(f"Carregando matriz {cache_path.name} ...", flush=True)
    with gzip.open(cache_path, "rt", encoding="utf-8") as source:
        payload = json.load(source)
    index = {int(item["ibge_code"]): position for position, item in enumerate(payload["municipalities"])}
    costs = {metric: payload[metric]["costs"] for metric in ("distance", "time")}
    paths = {metric: payload[metric]["paths"] for metric in ("distance", "time")}
    return index, costs, paths, manifest["cache_key"]


def backfill_legs(
    project_root: Path = PROJECT_ROOT,
    *,
    run_ids: set[str] | None = None,
    force: bool = False,
) -> int:
    jobs_dir = project_root / "data/processed/jobs"
    pending: list[tuple[Path, dict[str, Any]]] = []
    for result_path in sorted(jobs_dir.glob("*.json")):
        if result_path.name.endswith(".preview.json"):
            continue
        if run_ids is not None and result_path.stem not in run_ids:
            continue
        if legs_sidecar_path(result_path).is_file() and not force:
            continue
        try:
            report = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if any(_scenario_routes(item) for item in report.get("scenarios", [])):
            pending.append((result_path, report))
    if not pending:
        print("Nenhum resultado sem geometria viária.")
        return 0

    index, costs, paths, cache_key = _load_matrix(project_root)
    written = 0
    for result_path, report in pending:
        fingerprint_key = report.get("fingerprint", {}).get("matrix_cache_key")
        if fingerprint_key != cache_key:
            print(f"{result_path.name}: matriz do resultado diverge da atual; ignorado", file=sys.stderr)
            continue
        routes_out: list[dict[str, Any]] = []
        for scenario in report.get("scenarios", []):
            metric = scenario.get("objective")
            if metric not in ("distance", "time"):
                continue
            for route in _scenario_routes(scenario):
                visit_order = [int(code) for code in route["visit_order"]]
                leg_edge_ids: list[list[str]] = []
                total = 0.0
                for source_code, target_code in zip(visit_order, visit_order[1:], strict=False):
                    row, column = index[source_code], index[target_code]
                    path = paths[metric][row][column]
                    cost = costs[metric][row][column]
                    if path is None or cost is None:
                        raise ValueError(f"Par inalcançável {source_code}->{target_code}")
                    leg_edge_ids.append(path)
                    total += cost
                expected = route["expected_cost"]
                if isinstance(expected, (int, float)) and not math.isclose(
                    total, expected, rel_tol=COST_TOLERANCE, abs_tol=1e-3
                ):
                    print(
                        f"{result_path.name} {scenario['scenario']}: custo da matriz {total:.3f} "
                        f"diverge de {expected:.3f}; rota ignorada",
                        file=sys.stderr,
                    )
                    continue
                routes_out.append(
                    {
                        "scenario": scenario["scenario"],
                        "visit_order": visit_order,
                        "leg_edge_ids": leg_edge_ids,
                    }
                )
        if not routes_out:
            continue
        target = legs_sidecar_path(result_path)
        temporary = target.with_name(target.name + ".tmp")
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as output:
            json.dump(
                {
                    "format_version": FORMAT_VERSION,
                    "matrix_cache_key": cache_key,
                    "routes": routes_out,
                },
                output,
                separators=(",", ":"),
            )
        temporary.replace(target)
        # O GeoJSON em cache foi montado com linhas retas; precisa ser refeito.
        for stale in jobs_dir.glob(f"{result_path.stem}-*.geojson"):
            stale.unlink()
        written += 1
        print(f"{result_path.name}: {len(routes_out)} rota(s) com geometria viária")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", action="append", help="limita a execuções específicas")
    parser.add_argument("--force", action="store_true", help="refaz arquivos auxiliares existentes")
    args = parser.parse_args()
    backfill_legs(run_ids=set(args.run_id) if args.run_id else None, force=args.force)


if __name__ == "__main__":
    main()
