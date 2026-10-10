from __future__ import annotations

import argparse
import gc
import gzip
import json
import math
import os
import platform
import resource
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

from pyproj import Geod

from app.core.memory_limit import enforce_memory_limit
from app.graph.builder import PROJECT_ROOT, file_sha256
from app.graph.network import RoadNetwork
from app.shortest_path.fast import FastDijkstra, NativeSearchUnavailable
from app.shortest_path.search import PathMetric, all_reachable_targets

GEOD = Geod(ellps="WGS84")
SNAP_GRID_DEGREES = 0.02
SNAP_WARNING_DISTANCE_M = 10_000.0
MATRIX_ALGORITHM_VERSION = "dijkstra-directed-v2"


@dataclass(frozen=True, slots=True)
class SeatSnap:
    ibge_code: int
    municipality_name: str
    seat_longitude: float
    seat_latitude: float
    node_id: int
    snap_distance_m: float
    within_10km: bool


@dataclass(slots=True)
class MetricMatrix:
    metric: PathMetric
    unit: str
    costs: list[list[float | None]]
    paths: list[list[list[str] | None]]

    @property
    def unreachable_directed_pairs(self) -> int:
        return sum(cost is None for row in self.costs for cost in row)


def _read_matrix_row_checkpoint(
    path: Path,
    *,
    cache_key: str,
    metric: PathMetric,
    source_ibge_code: int,
    target_count: int,
) -> tuple[list[float | None], list[list[str] | None]] | None:
    if not path.is_file():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as source:
            payload = json.load(source)
        costs = payload["costs"]
        paths = payload["paths"]
        if (
            payload.get("cache_key") != cache_key
            or payload.get("metric") != metric
            or payload.get("source_ibge_code") != source_ibge_code
            or not isinstance(costs, list)
            or not isinstance(paths, list)
            or len(costs) != target_count
            or len(paths) != target_count
        ):
            return None
        for cost, edge_ids in zip(costs, paths, strict=True):
            if cost is None:
                if edge_ids is not None:
                    return None
            elif (
                isinstance(cost, bool)
                or not isinstance(cost, (int, float))
                or not math.isfinite(cost)
                or cost < 0
                or not isinstance(edge_ids, list)
                or any(not isinstance(edge_id, str) for edge_id in edge_ids)
            ):
                return None
        return costs, paths
    except (OSError, EOFError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_matrix_row_checkpoint(
    path: Path,
    *,
    cache_key: str,
    metric: PathMetric,
    source_ibge_code: int,
    costs: list[float | None],
    paths: list[list[str] | None],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = {
        "cache_key": cache_key,
        "metric": metric,
        "source_ibge_code": source_ibge_code,
        "costs": costs,
        "paths": paths,
    }
    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            compressed.write(
                json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
            )
    temporary.replace(path)


def _distance_m(longitude_a: float, latitude_a: float, longitude_b: float, latitude_b: float) -> float:
    _, _, distance = GEOD.inv(longitude_a, latitude_a, longitude_b, latitude_b)
    return distance


def snap_seats(
    features: list[dict[str, Any]],
    network: RoadNetwork,
) -> list[SeatSnap]:
    if not network.nodes:
        raise ValueError("Não é possível encaixar sedes em um grafo vazio")

    grid: dict[tuple[int, int], list[int]] = {}
    for node_id, node in network.nodes.items():
        cell = (
            math.floor(node.longitude / SNAP_GRID_DEGREES),
            math.floor(node.latitude / SNAP_GRID_DEGREES),
        )
        grid.setdefault(cell, []).append(node_id)

    maximum_x = max(cell[0] for cell in grid)
    maximum_y = max(cell[1] for cell in grid)
    minimum_x = min(cell[0] for cell in grid)
    minimum_y = min(cell[1] for cell in grid)
    maximum_abs_latitude = max(abs(node.latitude) for node in network.nodes.values())
    result: list[SeatSnap] = []
    seen_codes: set[int] = set()

    for feature in features:
        if not isinstance(feature, dict):
            raise ValueError("Registro de sede GeoJSON inválido")
        properties = feature.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("Sede sem propriedades GeoJSON válidas")
        code = properties.get("ibge_code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise ValueError("Sede sem código IBGE inteiro válido")
        if not 5_100_000 <= code <= 5_199_999:
            raise ValueError(f"Código IBGE não pertence a Mato Grosso: {code}")
        if code in seen_codes:
            raise ValueError(f"Código IBGE duplicado no GeoJSON: {code}")
        seen_codes.add(code)
        name = properties.get("municipality_name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Sede {code} sem nome municipal válido")
        geometry = feature.get("geometry")
        coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if (
            not isinstance(geometry, dict)
            or geometry.get("type") != "Point"
            or not isinstance(coordinates, list)
            or len(coordinates) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in coordinates
            )
        ):
            raise ValueError(f"Sede sem ponto válido: {code}")
        longitude, latitude = float(coordinates[0]), float(coordinates[1])
        if (
            not math.isfinite(longitude)
            or not math.isfinite(latitude)
            or not -180 <= longitude <= 180
            or not -90 <= latitude <= 90
        ):
            raise ValueError(f"Sede com coordenadas inválidas: {code}")
        center_x = math.floor(longitude / SNAP_GRID_DEGREES)
        center_y = math.floor(latitude / SNAP_GRID_DEGREES)
        best_node: int | None = None
        best_distance = math.inf

        maximum_ring = max(
            abs(maximum_x - center_x),
            abs(minimum_x - center_x),
            abs(maximum_y - center_y),
            abs(minimum_y - center_y),
        )
        for ring in range(maximum_ring + 1):
            for x in range(center_x - ring, center_x + ring + 1):
                for y in range(center_y - ring, center_y + ring + 1):
                    if ring and center_x - ring < x < center_x + ring and center_y - ring < y < center_y + ring:
                        continue
                    for node_id in grid.get((x, y), ()):
                        node = network.nodes[node_id]
                        distance = _distance_m(longitude, latitude, node.longitude, node.latitude)
                        if distance < best_distance:
                            best_distance = distance
                            best_node = node_id

            if best_node is not None:
                west = (center_x - ring) * SNAP_GRID_DEGREES
                east = (center_x + ring + 1) * SNAP_GRID_DEGREES
                south = (center_y - ring) * SNAP_GRID_DEGREES
                north = (center_y + ring + 1) * SNAP_GRID_DEGREES
                lat_lower_bound = min(latitude - south, north - latitude) * 109_000
                lon_scale = (
                    109_000
                    * max(
                        0.1,
                        math.cos(
                            math.radians(max(maximum_abs_latitude, abs(latitude)))
                        ),
                    )
                    * 0.99
                )
                lon_lower_bound = min(longitude - west, east - longitude) * lon_scale
                outside_lower_bound = max(0.0, min(lat_lower_bound, lon_lower_bound))
                if best_distance <= outside_lower_bound:
                    break

        if best_node is None:
            raise ValueError(f"Nenhum nó viário próximo da sede IBGE {code}")
        result.append(
            SeatSnap(
                ibge_code=code,
                municipality_name=name,
                seat_longitude=longitude,
                seat_latitude=latitude,
                node_id=best_node,
                snap_distance_m=best_distance,
                within_10km=best_distance <= SNAP_WARNING_DISTANCE_M,
            )
        )

    if len(result) != 142:
        raise ValueError(f"Esperadas 142 sedes para encaixe; encontradas {len(result)}")
    return sorted(result, key=lambda snap: snap.ibge_code)


def _unreachable_seats(
    costs: list[list[float | None]],
    snaps: list[SeatSnap],
) -> dict[str, list[dict[str, object]]]:
    outbound: list[dict[str, object]] = []
    inbound: list[dict[str, object]] = []
    for index, snap in enumerate(snaps):
        unreachable_destinations = [
            snaps[target].ibge_code for target, value in enumerate(costs[index]) if value is None
        ]
        unreachable_sources = [
            snaps[source].ibge_code
            for source, row in enumerate(costs)
            if row[index] is None
        ]
        if unreachable_destinations:
            outbound.append(
                {
                    "ibge_code": snap.ibge_code,
                    "municipality_name": snap.municipality_name,
                    "unreachable_destinations": unreachable_destinations,
                }
            )
        if unreachable_sources:
            inbound.append(
                {
                    "ibge_code": snap.ibge_code,
                    "municipality_name": snap.municipality_name,
                    "unreachable_sources": unreachable_sources,
                }
            )
    return {"unreachable_outbound": outbound, "unreachable_inbound": inbound}


def build_metric_matrix(
    network: RoadNetwork,
    snaps: list[SeatSnap],
    metric: PathMetric,
    *,
    engine: FastDijkstra | None = None,
    workers: int | None = None,
    checkpoint_dir: Path | None = None,
    checkpoint_key: str | None = None,
    progress: bool = False,
    progress_label: str | None = None,
) -> MetricMatrix:
    if metric not in {"distance", "time"}:
        raise ValueError(f"Métrica desconhecida: {metric}")
    if not snaps:
        raise ValueError("A matriz requer pelo menos uma sede")
    unique_target_nodes = list(dict.fromkeys(snap.node_id for snap in snaps))
    target_index = {node_id: index for index, node_id in enumerate(unique_target_nodes)}
    row_costs: list[list[float | None] | None] = [None] * len(snaps)
    row_paths: list[list[list[str] | None] | None] = [None] * len(snaps)
    pending: list[int] = []

    if checkpoint_dir is not None:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
    for source_index, snap in enumerate(snaps):
        checkpoint_path = (
            checkpoint_dir / f"{snap.ibge_code}.json.gz"
            if checkpoint_dir is not None
            else None
        )
        cached_row = (
            _read_matrix_row_checkpoint(
                checkpoint_path,
                cache_key=checkpoint_key,
                metric=metric,
                source_ibge_code=snap.ibge_code,
                target_count=len(snaps),
            )
            if checkpoint_path is not None and checkpoint_key is not None
            else None
        )
        if cached_row is None:
            pending.append(source_index)
        else:
            row_costs[source_index], row_paths[source_index] = cached_row

    worker_count = (
        max(1, min(workers or (os.cpu_count() or 1), len(pending)))
        if pending and engine is not None
        else (1 if pending else 0)
    )
    label = progress_label or f"Matriz {metric}"
    started_at = time.perf_counter()
    completed = len(snaps) - len(pending)
    if progress:
        print(
            f"{label}: {completed}/{len(snaps)} origens recuperadas; "
            f"{len(pending)} pendentes; {worker_count} worker(s)",
            flush=True,
        )

    def compute_row(source_index: int) -> tuple[list[float | None], list[list[str] | None]]:
        source = snaps[source_index].node_id
        if engine is not None:
            unique_costs, unique_paths = engine.search_source(
                source,
                unique_target_nodes,
                metric,
            )
        else:
            reachable = all_reachable_targets(
                network,
                source,
                set(unique_target_nodes),
                metric,
            )
            unique_costs = [
                reachable[node_id].cost if node_id in reachable else None
                for node_id in unique_target_nodes
            ]
            unique_paths = [
                list(reachable[node_id].edge_ids) if node_id in reachable else None
                for node_id in unique_target_nodes
            ]
        return (
            [unique_costs[target_index[snap.node_id]] for snap in snaps],
            [unique_paths[target_index[snap.node_id]] for snap in snaps],
        )

    def keep_row(source_index: int, costs: list[float | None], paths: list[list[str] | None]) -> None:
        row_costs[source_index] = costs
        row_paths[source_index] = paths
        if checkpoint_dir is not None and checkpoint_key is not None:
            _write_matrix_row_checkpoint(
                checkpoint_dir / f"{snaps[source_index].ibge_code}.json.gz",
                cache_key=checkpoint_key,
                metric=metric,
                source_ibge_code=snaps[source_index].ibge_code,
                costs=costs,
                paths=paths,
            )

    def report_progress() -> None:
        if not progress:
            return
        elapsed = time.perf_counter() - started_at
        remaining = (elapsed / completed * (len(snaps) - completed)) if completed else 0.0
        print(
            f"{label}: {completed}/{len(snaps)} origens "
            f"({completed / len(snaps):.0%}); {elapsed:.0f}s decorridos, "
            f"ETA ~{remaining:.0f}s",
            flush=True,
        )

    if engine is not None and worker_count > 1 and len(pending) > 1:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="mt-dijkstra") as pool:
            futures = {pool.submit(compute_row, index): index for index in pending}
            for future in as_completed(futures):
                source_index = futures[future]
                costs, paths = future.result()
                keep_row(source_index, costs, paths)
                completed += 1
                report_progress()
    else:
        for source_index in pending:
            costs, paths = compute_row(source_index)
            keep_row(source_index, costs, paths)
            completed += 1
            report_progress()

    if any(row is None for row in row_costs) or any(row is None for row in row_paths):
        raise RuntimeError(f"Matriz {metric} terminou com linhas ausentes")
    return MetricMatrix(
        metric=metric,
        unit="m" if metric == "distance" else "s",
        costs=[row for row in row_costs if row is not None],
        paths=[row for row in row_paths if row is not None],
    )


def matrix_cache_key(
    graph_sha256: str,
    snaps: list[SeatSnap],
    speed_profile: dict[str, float],
) -> str:
    payload = {
        "graph_sha256": graph_sha256,
        "seats": [
            [
                snap.ibge_code,
                snap.municipality_name,
                snap.seat_longitude,
                snap.seat_latitude,
                snap.node_id,
                round(snap.snap_distance_m, 3),
            ]
            for snap in snaps
        ],
        "speed_profile": speed_profile,
        "algorithm": MATRIX_ALGORITHM_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _write_matrix_cache(
    path: Path,
    cache_key: str,
    graph_sha256: str,
    snaps: list[SeatSnap],
    distance: MetricMatrix,
    travel_time: MetricMatrix,
) -> None:
    payload = {
        "format_version": 1,
        "cache_key": cache_key,
        "graph_sha256": graph_sha256,
        "municipalities": [asdict(snap) for snap in snaps],
        "distance": asdict(distance),
        "time": asdict(travel_time),
        "limitations": [
            "Costs exclude off-road connectors between official seats and snapped road nodes.",
            "Turn restrictions via node are enforced; via-way restrictions are not modeled.",
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"))
            for chunk in encoder.iterencode(payload):
                compressed.write(chunk.encode())
    temporary.replace(path)


def _validate_manifested_cache_digest(
    cache_path: Path,
    *,
    cache_key: str,
    manifest_path: Path,
) -> None:
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Manifesto do cache imutável possui formato inválido")
    if manifest.get("cache_key") != cache_key:
        return
    expected_sha256 = manifest.get("cache_sha256")
    if not isinstance(expected_sha256, str) or file_sha256(cache_path) != expected_sha256:
        raise ValueError("Cache imutável diverge do SHA-256 registrado no manifesto")


def _validate_matrix_cache(
    payload: dict[str, Any],
    *,
    cache_key: str,
    graph_sha256: str,
    snaps: list[SeatSnap],
    network: RoadNetwork,
) -> tuple[int, int]:
    if (
        payload.get("format_version") != 1
        or payload.get("cache_key") != cache_key
        or payload.get("graph_sha256") != graph_sha256
    ):
        raise ValueError("Cache imutável possui cabeçalho incompatível")
    if payload.get("municipalities") != [asdict(snap) for snap in snaps]:
        raise ValueError("Cache imutável possui sedes ou encaixes incompatíveis")

    def validate_metric(metric: PathMetric, unit: str) -> int:
        matrix = payload.get(metric)
        count = len(snaps)
        if (
            not isinstance(matrix, dict)
            or matrix.get("metric") != metric
            or matrix.get("unit") != unit
        ):
            raise ValueError(f"Cache imutável possui metadados inválidos para {metric}")
        costs = matrix.get("costs")
        paths = matrix.get("paths")
        if (
            not isinstance(costs, list)
            or not isinstance(paths, list)
            or len(costs) != count
            or len(paths) != count
        ):
            raise ValueError(f"Cache imutável possui dimensões inválidas para {metric}")

        unreachable = 0
        for source_index, (cost_row, path_row) in enumerate(zip(costs, paths, strict=True)):
            if (
                not isinstance(cost_row, list)
                or not isinstance(path_row, list)
                or len(cost_row) != count
                or len(path_row) != count
            ):
                raise ValueError(f"Cache imutável possui linha inválida para {metric}")
            for target_index, (cost, edge_ids) in enumerate(
                zip(cost_row, path_row, strict=True)
            ):
                if cost is None:
                    if edge_ids is not None:
                        raise ValueError(f"Cache imutável diverge custo e caminho para {metric}")
                    unreachable += 1
                    continue
                if (
                    isinstance(cost, bool)
                    or not isinstance(cost, (int, float))
                    or not math.isfinite(cost)
                    or cost < 0
                    or not isinstance(edge_ids, list)
                    or any(not isinstance(edge_id, str) for edge_id in edge_ids)
                ):
                    raise ValueError(f"Cache imutável possui custo ou caminho inválido para {metric}")

                current_node = snaps[source_index].node_id
                incoming_way_id: int | None = None
                path_cost = 0.0
                for edge_id in edge_ids:
                    edge = network.edges.get(edge_id)
                    if edge is None or edge.source != current_node:
                        raise ValueError(f"Cache imutável possui trajetória descontínua para {metric}")
                    if not network.allows_turn(incoming_way_id, current_node, edge.way_id):
                        raise ValueError(f"Cache imutável viola restrição de conversão para {metric}")
                    path_cost += edge.length_m if metric == "distance" else edge.travel_time_s
                    current_node = edge.target
                    incoming_way_id = edge.way_id
                if current_node != snaps[target_index].node_id or not math.isclose(
                    float(cost), path_cost, rel_tol=1e-9, abs_tol=1e-6
                ):
                    raise ValueError(f"Cache imutável diverge trajetória e custo para {metric}")
                if (
                    snaps[source_index].node_id == snaps[target_index].node_id
                    and (cost != 0 or edge_ids)
                ):
                    raise ValueError(f"Cache imutável possui diagonal inválida para {metric}")
        return unreachable

    return validate_metric("distance", "m"), validate_metric("time", "s")


def _build_metric_pair(
    network: RoadNetwork,
    snaps: list[SeatSnap],
    *,
    engine: FastDijkstra | None = None,
    workers: int | None = None,
    checkpoint_dir: Path | None = None,
    checkpoint_key: str | None = None,
    progress: bool = False,
    progress_label: str = "Matriz",
) -> tuple[MetricMatrix, MetricMatrix]:
    return (
        build_metric_matrix(
            network,
            snaps,
            "distance",
            engine=engine,
            workers=workers,
            checkpoint_dir=checkpoint_dir / "distance" if checkpoint_dir else None,
            checkpoint_key=checkpoint_key,
            progress=progress,
            progress_label=f"{progress_label} · distância",
        ),
        build_metric_matrix(
            network,
            snaps,
            "time",
            engine=engine,
            workers=workers,
            checkpoint_dir=checkpoint_dir / "time" if checkpoint_dir else None,
            checkpoint_key=checkpoint_key,
            progress=progress,
            progress_label=f"{progress_label} · tempo",
        ),
    )


def _code_order_spaced_snaps(snaps: list[SeatSnap], count: int) -> list[SeatSnap]:
    if count < 1 or count > len(snaps):
        raise ValueError("A amostra deve conter entre 1 e o total de sedes")
    if count == 1:
        return [snaps[len(snaps) // 2]]
    indexes = [(index * (len(snaps) - 1)) // (count - 1) for index in range(count)]
    return [snaps[index] for index in indexes]


def build_and_persist_matrices(
    *,
    benchmark: bool = False,
    workers: int | None = None,
    progress: bool = True,
) -> dict[str, object]:
    graph_manifest_path = PROJECT_ROOT / "data/manifests/road-network-mt.json"
    graph_manifest = json.loads(graph_manifest_path.read_text(encoding="utf-8"))
    seat_manifest_path = PROJECT_ROOT / "data/manifests/municipalities-mt.json"
    seat_manifest = json.loads(seat_manifest_path.read_text(encoding="utf-8"))
    seat_source = seat_manifest["municipal_seats"]
    seats_path = PROJECT_ROOT / seat_source["output_path"]
    seat_catalog_sha256 = file_sha256(seats_path)
    if seat_catalog_sha256 != seat_source["output_sha256"]:
        raise ValueError("Hash do catálogo de sedes diverge do manifesto D2")

    graph_path = PROJECT_ROOT / graph_manifest["graph_path"]
    actual_graph_sha256 = file_sha256(graph_path)
    if actual_graph_sha256 != graph_manifest["graph_sha256"]:
        raise ValueError("Hash do grafo diverge do manifesto D3.1")
    if progress:
        print("Carregando grafo viário para matrizes...", flush=True)
    network = RoadNetwork.from_jsonl_gz(graph_path)
    seats_payload = json.loads(seats_path.read_text(encoding="utf-8"))
    snaps = snap_seats(seats_payload["features"], network)

    speed_profile = graph_manifest["quality"]["speed_defaults_kph"]
    cache_key = matrix_cache_key(actual_graph_sha256, snaps, speed_profile)
    cache_path = PROJECT_ROOT / f"data/matrices/{cache_key}.json.gz"
    cache_manifest_path = PROJECT_ROOT / "data/manifests/travel-matrix-mt.json"
    started_at = perf_counter()
    worker_count = max(1, workers or (os.cpu_count() or 1))
    engine: FastDijkstra | None = None
    engine_name = "cached"
    checkpoint_dirs: set[Path] = set()

    if benchmark or not cache_path.exists():
        try:
            if progress:
                print("Preparando arrays compactos e compilando Dijkstra C++...", flush=True)
            engine = FastDijkstra.from_network(network, {snap.node_id for snap in snaps})
            engine_name = "native-cpp"
            worker_count = min(worker_count, len(snaps))
        except NativeSearchUnavailable as error:
            engine_name = "python-reference"
            worker_count = 1
            if progress:
                print(f"Acelerador indisponível ({error}); usando Dijkstra Python.", flush=True)

    benchmark_results: list[dict[str, object]] = []
    if benchmark:
        distance: MetricMatrix | None = None
        travel_time: MetricMatrix | None = None
        for size in (5, 10, 20, 50, len(snaps)):
            subset = _code_order_spaced_snaps(snaps, size)
            subset_key = matrix_cache_key(actual_graph_sha256, subset, speed_profile)
            subset_checkpoint_dir = PROJECT_ROOT / f"data/matrices/.work-{subset_key}"
            checkpoint_dirs.add(subset_checkpoint_dir)
            checkpoint_rows_reused = sum(
                (subset_checkpoint_dir / metric / f"{snap.ibge_code}.json.gz").is_file()
                for metric in ("distance", "time")
                for snap in subset
            )
            phase_started = perf_counter()
            distance, travel_time = _build_metric_pair(
                network,
                subset,
                engine=engine,
                workers=worker_count,
                checkpoint_dir=subset_checkpoint_dir,
                checkpoint_key=subset_key,
                progress=progress,
                progress_label=f"Benchmark {size}/{len(snaps)} sedes",
            )
            benchmark_results.append(
                {
                    "municipality_count": size,
                    "municipality_codes": [snap.ibge_code for snap in subset],
                    "graph_node_count": len(network.nodes),
                    "graph_directed_edge_count": len(network.edges),
                    "engine": engine_name,
                    "worker_count": min(worker_count, size) if engine else 1,
                    "checkpoint_rows_reused": checkpoint_rows_reused,
                    "timing_valid": checkpoint_rows_reused == 0,
                    "elapsed_seconds": round(perf_counter() - phase_started, 3),
                    "process_peak_rss_mib": round(
                        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                        2,
                    ),
                    "distance_unreachable_directed_pairs": distance.unreachable_directed_pairs,
                    "time_unreachable_directed_pairs": travel_time.unreachable_directed_pairs,
                }
            )
            if size == len(snaps):
                if not cache_path.exists():
                    _write_matrix_cache(
                        cache_path,
                        cache_key,
                        actual_graph_sha256,
                        snaps,
                        distance,
                        travel_time,
                    )
                distance_unreachable = distance.unreachable_directed_pairs
                time_unreachable = travel_time.unreachable_directed_pairs
                distance_reachability = _unreachable_seats(distance.costs, snaps)
                time_reachability = _unreachable_seats(travel_time.costs, snaps)
            del distance, travel_time
            gc.collect()
        if cache_path.exists():
            _validate_manifested_cache_digest(
                cache_path,
                cache_key=cache_key,
                manifest_path=cache_manifest_path,
            )
            with gzip.open(cache_path, "rt", encoding="utf-8") as source:
                existing_cache = json.load(source)
            _validate_matrix_cache(
                existing_cache,
                cache_key=cache_key,
                graph_sha256=actual_graph_sha256,
                snaps=snaps,
                network=network,
            )
        cache_hit = False
        benchmark_report_path = PROJECT_ROOT / "data/processed/matrix-benchmarks-mt.json"
        benchmark_report_path.parent.mkdir(parents=True, exist_ok=True)
        benchmark_report_path.write_text(
            json.dumps(
                {
                    "graph_sha256": actual_graph_sha256,
                    "seat_catalog_sha256": seat_catalog_sha256,
                    "methodology": {
                        "algorithm": "Dijkstra",
                        "algorithm_version": MATRIX_ALGORITHM_VERSION,
                        "seat_sampling": (
                            "Equally spaced indices in ascending IBGE code order; "
                            "not spatially balanced."
                        ),
                        "repetitions_per_size": 1,
                    },
                    "runtime_environment": {
                        "python_version": platform.python_version(),
                        "platform": platform.platform(),
                        "engine": engine_name,
                        "worker_count": worker_count,
                        "native_compiler": engine.compiler_details if engine else None,
                    },
                    "benchmarks": benchmark_results,
                    "process_peak_rss_note": (
                        "RSS is the process high-water mark; graph remains resident for every size."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    elif cache_path.exists():
        _validate_manifested_cache_digest(
            cache_path,
            cache_key=cache_key,
            manifest_path=cache_manifest_path,
        )
        with gzip.open(cache_path, "rt", encoding="utf-8") as source:
            cached = json.load(source)
        distance_unreachable, time_unreachable = _validate_matrix_cache(
            cached,
            cache_key=cache_key,
            graph_sha256=actual_graph_sha256,
            snaps=snaps,
            network=network,
        )
        cache_hit = True
        distance_reachability = _unreachable_seats(cached["distance"]["costs"], snaps)
        time_reachability = _unreachable_seats(cached["time"]["costs"], snaps)
    else:
        checkpoint_dir = PROJECT_ROOT / f"data/matrices/.work-{cache_key}"
        checkpoint_dirs.add(checkpoint_dir)
        distance, travel_time = _build_metric_pair(
            network,
            snaps,
            engine=engine,
            workers=worker_count,
            checkpoint_dir=checkpoint_dir,
            checkpoint_key=cache_key,
            progress=progress,
            progress_label=f"Matriz completa ({len(snaps)} sedes)",
        )
        _write_matrix_cache(
            cache_path,
            cache_key,
            actual_graph_sha256,
            snaps,
            distance,
            travel_time,
        )
        cache_hit = False
        distance_unreachable = distance.unreachable_directed_pairs
        time_unreachable = travel_time.unreachable_directed_pairs
        distance_reachability = _unreachable_seats(distance.costs, snaps)
        time_reachability = _unreachable_seats(travel_time.costs, snaps)

    snap_report = {
        "graph_sha256": actual_graph_sha256,
        "municipality_count": len(snaps),
        "snapped_within_10km": sum(snap.within_10km for snap in snaps),
        "maximum_snap_distance_m": max(snap.snap_distance_m for snap in snaps),
        "mean_snap_distance_m": sum(snap.snap_distance_m for snap in snaps) / len(snaps),
        "snap_distances": [asdict(snap) for snap in snaps],
        "unreachable_distance_pairs": distance_unreachable,
        "unreachable_time_pairs": time_unreachable,
        "distance_reachability": distance_reachability,
        "time_reachability": time_reachability,
    }
    snap_report_path = PROJECT_ROOT / "data/processed/municipality-road-snaps.json"
    snap_report_path.write_text(
        json.dumps(snap_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "dataset": "Mato Grosso directed road travel matrices",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "graph_sha256": actual_graph_sha256,
        "seat_catalog_sha256": seat_catalog_sha256,
        "cache_key": cache_key,
        "cache_path": str(cache_path.relative_to(PROJECT_ROOT)),
        "cache_sha256": file_sha256(cache_path),
        "cache_hit": cache_hit,
        "routing_engine": engine_name,
        "worker_count": worker_count if engine_name != "cached" else 0,
        "native_compiler": engine.compiler_details if engine else None,
        "municipality_count": len(snaps),
        "distance": {"unit": "m", "unreachable_directed_pairs": distance_unreachable},
        "time": {"unit": "s", "unreachable_directed_pairs": time_unreachable},
        "snap_report_path": str(snap_report_path.relative_to(PROJECT_ROOT)),
        "elapsed_seconds": round(perf_counter() - started_at, 3),
        "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
        "benchmark_requested": benchmark,
        "benchmark_report_path": (
            "data/processed/matrix-benchmarks-mt.json" if benchmark else None
        ),
    }
    cache_manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for checkpoint_dir in checkpoint_dirs:
        shutil.rmtree(checkpoint_dir, ignore_errors=True)
    return manifest


def main() -> None:
    enforce_memory_limit(
        command=[sys.executable, "-m", "app.routing.matrices", *sys.argv[1:]],
    )
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Run scoped matrix benchmarks for 5, 10, 20, 50, and all seats.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="Parallel native Dijkstra workers (default: logical CPU count).",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            build_and_persist_matrices(benchmark=args.benchmark, workers=args.workers),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
