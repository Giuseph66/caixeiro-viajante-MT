from __future__ import annotations

import argparse
import gc
import gzip
import json
import math
import platform
import resource
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

from pyproj import Geod

from app.graph.builder import PROJECT_ROOT, file_sha256
from app.graph.network import RoadNetwork
from app.shortest_path.search import PathMetric, RoadPath, all_reachable_targets

GEOD = Geod(ellps="WGS84")
SNAP_GRID_DEGREES = 0.02
SNAP_WARNING_DISTANCE_M = 10_000.0
MATRIX_ALGORITHM_VERSION = "dijkstra-directed-v1"


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


def _path_for_cost(
    paths: dict[int, RoadPath],
    destination: int,
) -> RoadPath | None:
    return paths.get(destination)


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
) -> MetricMatrix:
    if metric not in {"distance", "time"}:
        raise ValueError(f"Métrica desconhecida: {metric}")
    node_ids = [snap.node_id for snap in snaps]
    target_nodes = set(node_ids)
    costs: list[list[float | None]] = []
    paths: list[list[list[str] | None]] = []
    for source in node_ids:
        reachable = all_reachable_targets(network, source, target_nodes, metric)
        cost_row: list[float | None] = []
        path_row: list[list[str] | None] = []
        for destination in node_ids:
            result = _path_for_cost(reachable, destination)
            cost_row.append(result.cost if result else None)
            path_row.append(list(result.edge_ids) if result else None)
        costs.append(cost_row)
        paths.append(path_row)
    return MetricMatrix(
        metric=metric,
        unit="m" if metric == "distance" else "s",
        costs=costs,
        paths=paths,
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
) -> tuple[MetricMatrix, MetricMatrix]:
    return (
        build_metric_matrix(network, snaps, "distance"),
        build_metric_matrix(network, snaps, "time"),
    )


def _code_order_spaced_snaps(snaps: list[SeatSnap], count: int) -> list[SeatSnap]:
    if count < 1 or count > len(snaps):
        raise ValueError("A amostra deve conter entre 1 e o total de sedes")
    if count == 1:
        return [snaps[len(snaps) // 2]]
    indexes = [(index * (len(snaps) - 1)) // (count - 1) for index in range(count)]
    return [snaps[index] for index in indexes]


def build_and_persist_matrices(*, benchmark: bool = False) -> dict[str, object]:
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
    network = RoadNetwork.from_jsonl_gz(graph_path)
    seats_payload = json.loads(seats_path.read_text(encoding="utf-8"))
    snaps = snap_seats(seats_payload["features"], network)

    speed_profile = graph_manifest["quality"]["speed_defaults_kph"]
    cache_key = matrix_cache_key(actual_graph_sha256, snaps, speed_profile)
    cache_path = PROJECT_ROOT / f"data/matrices/{cache_key}.json.gz"
    cache_manifest_path = PROJECT_ROOT / "data/manifests/travel-matrix-mt.json"
    started_at = perf_counter()

    benchmark_results: list[dict[str, object]] = []
    if benchmark:
        distance: MetricMatrix | None = None
        travel_time: MetricMatrix | None = None
        for size in (5, 10, 20, 50, len(snaps)):
            subset = _code_order_spaced_snaps(snaps, size)
            phase_started = perf_counter()
            distance, travel_time = _build_metric_pair(network, subset)
            benchmark_results.append(
                {
                    "municipality_count": size,
                    "municipality_codes": [snap.ibge_code for snap in subset],
                    "graph_node_count": len(network.nodes),
                    "graph_directed_edge_count": len(network.edges),
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
        distance, travel_time = _build_metric_pair(network, snaps)
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
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Enable benchmark mode; requires separate resource approval.",
    )
    args = parser.parse_args()
    print(json.dumps(build_and_persist_matrices(benchmark=args.benchmark), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
