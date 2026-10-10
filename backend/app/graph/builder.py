from __future__ import annotations

import json
import math
import resource
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from time import perf_counter
from zipfile import ZipFile

import osmium
import shapefile
from pyproj import CRS, Geod, Transformer, proj_version_str

from app.core.memory_limit import enforce_memory_limit
from app.graph.network import RoadEdge, RoadNetwork, RoadNode, TurnRestriction

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ROAD_CLASSES = frozenset(
    {
        "motorway",
        "motorway_link",
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
        "unclassified",
        "residential",
        "living_street",
        "service",
        "road",
        "track",
    }
)
DEFAULT_SPEED_KPH = {
    "motorway": 100.0,
    "motorway_link": 70.0,
    "trunk": 90.0,
    "trunk_link": 60.0,
    "primary": 80.0,
    "primary_link": 50.0,
    "secondary": 70.0,
    "secondary_link": 40.0,
    "tertiary": 60.0,
    "tertiary_link": 35.0,
    "unclassified": 40.0,
    "residential": 30.0,
    "living_street": 10.0,
    "service": 20.0,
    "road": 30.0,
    "track": 25.0,
}
RESTRICTED_ACCESS = frozenset(
    {
        "no",
        "private",
        "restricted",
        "agricultural",
        "forestry",
        "delivery",
        "destination",
        "permit",
        "customers",
        "residents",
        "employees",
        "military",
        "emergency",
        "bus",
        "psv",
        "hgv",
        "taxi",
        "motorcycle",
        "moped",
        "bicycle",
        "foot",
        "horse",
        "dismount",
        "use_sidepath",
        "disabled",
        "members",
        "goods",
        "carpool",
        "unknown",
    }
)
ALLOWED_ACCESS_VALUES = frozenset(
    {"yes", "permissive", "designated", "official", "none", "unrestricted", "motorcar"}
)
ACCESS_VALUE_ALIASES = {"não": "no", "sim": "yes"}
ACCESS_PRECEDENCE = ("motorcar", "motor_vehicle", "vehicle", "access")
RELEVANT_TAGS = (
    "access",
    "vehicle",
    "motor_vehicle",
    "motorcar",
    "oneway",
    "oneway:vehicle",
    "oneway:motor_vehicle",
    "oneway:motorcar",
    "maxspeed",
    "surface",
    "bridge",
    "tunnel",
    "lanes",
    "junction",
)
KNOWN_ACCESS_VALUES = RESTRICTED_ACCESS | ALLOWED_ACCESS_VALUES | frozenset(ACCESS_VALUE_ALIASES)
GEOD = Geod(ellps="WGS84")
BOUNDARY_PADDING_M = 100_000
ROAD_PROFILE_VERSION = "passenger_car-v3"


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_state_bounds(boundary_zip: Path) -> tuple[float, float, float, float]:
    with ZipFile(boundary_zip) as archive:
        shapefiles = [name for name in archive.namelist() if name.lower().endswith(".shp")]
        projections = [name for name in archive.namelist() if name.lower().endswith(".prj")]
        indexes = [name for name in archive.namelist() if name.lower().endswith(".shx")]
        if len(shapefiles) != 1 or len(projections) != 1 or len(indexes) != 1:
            raise ValueError("Pacote de limite do IBGE deve conter um SHP, SHX e PRJ")
        source_crs = CRS.from_wkt(archive.read(projections[0]).decode("utf-8")).to_epsg()
        if source_crs != 4674:
            raise ValueError(f"CRS do limite do IBGE mudou: EPSG:{source_crs}")
        reader = shapefile.Reader(
            shp=BytesIO(archive.read(shapefiles[0])),
            shx=BytesIO(archive.read(indexes[0])),
        )
        points = [point for shape in reader.iterShapes() for point in shape.points]
    if not points:
        raise ValueError("Limite estadual IBGE sem coordenadas")

    forward = Transformer.from_crs(4674, 5880, always_xy=True)
    inverse = Transformer.from_crs(5880, 4674, always_xy=True)
    eastings, northings = forward.transform(
        [point[0] for point in points],
        [point[1] for point in points],
    )
    x_min = min(eastings) - BOUNDARY_PADDING_M
    x_max = max(eastings) + BOUNDARY_PADDING_M
    y_min = min(northings) - BOUNDARY_PADDING_M
    y_max = max(northings) + BOUNDARY_PADDING_M
    samples = max(1, math.ceil(max(x_max - x_min, y_max - y_min) / 25_000))
    projected_x: list[float] = []
    projected_y: list[float] = []
    for index in range(samples + 1):
        fraction = index / samples
        x = x_min + fraction * (x_max - x_min)
        y = y_min + fraction * (y_max - y_min)
        projected_x.extend((x, x, x_min, x_max))
        projected_y.extend((y_min, y_max, y, y))
    longitudes, latitudes = inverse.transform(
        projected_x,
        projected_y,
    )
    return min(longitudes), min(latitudes), max(longitudes), max(latitudes)


def _tags_as_dict(tags: object) -> dict[str, str]:
    return {tag.k: tag.v for tag in tags}  # type: ignore[attr-defined]


def is_car_accessible(tags: dict[str, str]) -> tuple[bool, str | None]:
    highway = tags.get("highway")
    if highway not in ROAD_CLASSES:
        return False, None
    for key in ACCESS_PRECEDENCE:
        raw_value = tags.get(key)
        if raw_value is None:
            continue
        value = raw_value.strip().lower()
        values = {
            ACCESS_VALUE_ALIASES.get(item.strip(), item.strip())
            for item in value.split(";")
        }
        restricted = sorted(values & RESTRICTED_ACCESS)
        if restricted:
            return False, restricted[0]
        unrecognized = sorted(values - KNOWN_ACCESS_VALUES)
        if unrecognized:
            return False, f"unrecognized:{unrecognized[0]}"
        return True, None
    return True, None


def _speed_for_way(tags: dict[str, str], highway: str) -> tuple[float, str, bool]:
    raw = tags.get("maxspeed", "").strip().lower()
    if raw:
        first_value = raw.split(";", maxsplit=1)[0].strip()
        match = re.match(r"^(\d+(?:[.,]\d+)?)\s*(mph|km/h|kmh|kph)?$", first_value)
        if match:
            speed = float(match.group(1).replace(",", "."))
            if match.group(2):
                speed *= 1.609344 if match.group(2) == "mph" else 1.0
            if 0 < speed <= 250:
                return speed, "maxspeed", False
            return DEFAULT_SPEED_KPH[highway], "class_default", True
        return DEFAULT_SPEED_KPH[highway], "class_default", True
    return DEFAULT_SPEED_KPH[highway], "class_default", False


def _oneway_value(tags: dict[str, str]) -> str:
    return (
        tags.get("oneway:motorcar")
        or tags.get("oneway:motor_vehicle")
        or tags.get("oneway:vehicle")
        or tags.get("oneway", "")
    ).strip().lower()


def _directions(tags: dict[str, str]) -> tuple[bool, bool, bool]:
    value = _oneway_value(tags)
    if not value and tags.get("junction") in {"roundabout", "circular"}:
        value = "yes"
    if value in {"yes", "1", "true"}:
        return True, False, False
    if value == "-1":
        return False, True, False
    if value in {"no", "0", "false", ""}:
        return True, True, False
    return True, True, True


def _restriction_exempts_car(tags: dict[str, str]) -> bool:
    exceptions = {value.strip().lower() for value in tags.get("except", "").split(";")}
    return bool(exceptions.intersection({"motorcar", "motor_vehicle", "vehicle"}))


def _has_conditional_restriction(tags: dict[str, str]) -> bool:
    return any(key.startswith("restriction") and key.endswith(":conditional") for key in tags)


def _restriction_kind(tags: dict[str, str]) -> str | None:
    if _restriction_exempts_car(tags):
        return None
    for key in ("restriction:motorcar", "restriction:motor_vehicle", "restriction:vehicle", "restriction"):
        value = tags.get(key, "").strip().lower()
        if value.startswith("only_"):
            return "only"
        if value.startswith("no_"):
            return "no"
    return None


class _RegionalGraphHandler(osmium.SimpleHandler):
    def __init__(self, bounds: tuple[float, float, float, float]) -> None:
        super().__init__()
        self.bounds = bounds
        self.nodes: dict[int, RoadNode] = {}
        self.edges: dict[str, RoadEdge] = {}
        self.turn_restrictions: list[TurnRestriction] = []
        self.started_ways = False
        self.node_count = 0
        self.highway_way_count = 0
        self.car_way_count = 0
        self.in_scope_way_count = 0
        self.in_scope_way_ids: set[int] = set()
        self.missing_node_segments = 0
        self.car_highway_values: Counter[str] = Counter()
        self.in_scope_highway_values: Counter[str] = Counter()
        self.speed_source_values: Counter[str] = Counter()
        self.access_rejections: Counter[str] = Counter()
        self.access_unrecognized: Counter[str] = Counter()
        self.malformed_speed_count = 0
        self.unrecognized_oneway_values: Counter[str] = Counter()
        self.unsupported_turn_restriction_count = 0

    def node(self, node: osmium.osm.Node) -> None:
        if self.started_ways:
            raise ValueError("PBF fora da ordem esperada: nó encontrado após way")
        self.node_count += 1

    def way(self, way: osmium.osm.Way) -> None:
        self.started_ways = True
        tags = _tags_as_dict(way.tags)
        if "highway" not in tags:
            return
        self.highway_way_count += 1
        accessible, rejection = is_car_accessible(tags)
        effective_access = next(
            (tags[key].strip().lower() for key in ACCESS_PRECEDENCE if tags.get(key)),
            None,
        )
        access_values = (
            {item.strip() for item in effective_access.split(";")} if effective_access else set()
        )
        if access_values and not access_values <= KNOWN_ACCESS_VALUES:
            self.access_unrecognized[effective_access] += 1
        if not accessible:
            if rejection:
                self.access_rejections[rejection] += 1
            return
        self.car_way_count += 1
        highway = tags["highway"]
        self.car_highway_values[highway] += 1

        west, south, east, north = self.bounds
        way_nodes: list[tuple[int, tuple[float, float] | None]] = []
        in_scope = False
        for node in way.nodes:
            if node.location.valid():
                coordinates = (node.location.lon, node.location.lat)
                longitude, latitude = coordinates
                inside = west <= longitude <= east and south <= latitude <= north
                if inside:
                    in_scope = True
                way_nodes.append((node.ref, coordinates if inside else None))
            else:
                way_nodes.append((node.ref, None))
        if not in_scope:
            return
        self.in_scope_way_count += 1
        self.in_scope_way_ids.add(way.id)
        self.in_scope_highway_values[highway] += 1
        speed_kph, speed_source, malformed_speed = _speed_for_way(tags, highway)
        self.speed_source_values[speed_source] += 1
        self.malformed_speed_count += int(malformed_speed)
        forward, reverse, unrecognized_oneway = _directions(tags)
        if unrecognized_oneway:
            raw_oneway = _oneway_value(tags)
            self.unrecognized_oneway_values[raw_oneway or "unknown"] += 1
            return
        saved_tags = tuple((key, tags[key]) for key in RELEVANT_TAGS if key in tags)

        for index, ((source, source_coordinates), (target, target_coordinates)) in enumerate(
            zip(way_nodes, way_nodes[1:])
        ):
            if source_coordinates is None or target_coordinates is None:
                self.missing_node_segments += 1
                continue
            azimuth, _, length_m = GEOD.inv(
                source_coordinates[0],
                source_coordinates[1],
                target_coordinates[0],
                target_coordinates[1],
            )
            del azimuth
            if not math.isfinite(length_m) or length_m <= 0:
                continue
            self.nodes.setdefault(
                source,
                RoadNode(source, source_coordinates[0], source_coordinates[1]),
            )
            self.nodes.setdefault(
                target,
                RoadNode(target, target_coordinates[0], target_coordinates[1]),
            )
            travel_time_s = length_m / (speed_kph / 3.6)
            physical_id = f"osm-way:{way.id}:segment:{index}"
            if forward:
                edge = RoadEdge(
                    f"{physical_id}:forward",
                    source,
                    target,
                    way.id,
                    physical_id,
                    length_m,
                    travel_time_s,
                    highway,
                    speed_kph,
                    speed_source,
                    saved_tags,
                )
                self.edges[edge.edge_id] = edge
            if reverse:
                edge = RoadEdge(
                    f"{physical_id}:reverse",
                    target,
                    source,
                    way.id,
                    physical_id,
                    length_m,
                    travel_time_s,
                    highway,
                    speed_kph,
                    speed_source,
                    saved_tags,
                )
                self.edges[edge.edge_id] = edge

    def relation(self, relation: osmium.osm.Relation) -> None:
        tags = _tags_as_dict(relation.tags)
        if not any(key.startswith("restriction") for key in tags):
            return
        from_ways = [member.ref for member in relation.members if member.role == "from" and member.type == "w"]
        to_ways = [member.ref for member in relation.members if member.role == "to" and member.type == "w"]
        via_nodes = [member.ref for member in relation.members if member.role == "via" and member.type == "n"]
        via_ways = [member.ref for member in relation.members if member.role == "via" and member.type == "w"]
        relevant = (
            any(node_id in self.nodes for node_id in via_nodes)
            or any(way_id in self.in_scope_way_ids for way_id in via_ways)
        )
        if not relevant:
            return
        if _restriction_exempts_car(tags):
            return
        if _has_conditional_restriction(tags):
            self.unsupported_turn_restriction_count += 1
            return
        kind = _restriction_kind(tags)
        if kind is None:
            return
        if len(from_ways) == len(to_ways) == len(via_nodes) == 1 and not via_ways:
            self.turn_restrictions.append(
                TurnRestriction(from_ways[0], via_nodes[0], to_ways[0], kind)
            )
        elif via_nodes or via_ways:
            self.unsupported_turn_restriction_count += 1


def build_road_network(
    pbf_path: Path,
    boundary_zip: Path,
    *,
    expected_pbf_sha256: str | None = None,
    expected_boundary_sha256: str | None = None,
) -> tuple[RoadNetwork, dict[str, object]]:
    if not pbf_path.is_file() or not boundary_zip.is_file():
        raise FileNotFoundError("PBF OSM ou limite oficial IBGE ausente")
    boundary_hash = file_sha256(boundary_zip)
    if expected_boundary_sha256 and boundary_hash != expected_boundary_sha256:
        raise ValueError("SHA-256 do limite IBGE diverge do manifesto D3")
    bounds = _read_state_bounds(boundary_zip)
    pbf_hash = file_sha256(pbf_path)
    if expected_pbf_sha256 and pbf_hash != expected_pbf_sha256:
        raise ValueError("SHA-256 do PBF diverge do manifesto D2")
    runtime_versions = {
        "pyosmium": version("osmium"),
        "pyproj": version("pyproj"),
        "proj": proj_version_str,
    }
    handler = _RegionalGraphHandler(bounds)
    processed_dir = PROJECT_ROOT / "data/processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    started_at = perf_counter()
    with tempfile.TemporaryDirectory(prefix="osm-node-locations-", dir=processed_dir) as temp_dir:
        index_path = Path(temp_dir) / "locations.idx"
        handler.apply_file(
            str(pbf_path),
            locations=True,
            idx=f"sparse_file_array,{index_path}",
        )
    elapsed_seconds = round(perf_counter() - started_at, 3)
    source_node_count = handler.node_count
    network = RoadNetwork(
        nodes=handler.nodes,
        edges=handler.edges,
        turn_restrictions=tuple(handler.turn_restrictions),
        metadata={
            "profile": "passenger_car",
            "profile_version": ROAD_PROFILE_VERSION,
            "source_pbf_sha256": pbf_hash,
            "bounds_wgs84": [round(value, 8) for value in bounds],
            "boundary_padding_m": BOUNDARY_PADDING_M,
            "boundary_crs": "EPSG:4674",
            "working_crs": "EPSG:5880",
            "runtime_versions": runtime_versions,
        },
    )
    if not network.nodes or not network.edges:
        raise ValueError("O PBF não produziu segmentos automotivos dentro do recorte")
    report: dict[str, object] = {
        "parser": "PyOsmium",
        "parser_version": runtime_versions["pyosmium"],
        "runtime_versions": runtime_versions,
        "road_profile_version": ROAD_PROFILE_VERSION,
        "elapsed_seconds": elapsed_seconds,
        "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
        "pbf_sha256": pbf_hash,
        "boundary_sha256": boundary_hash,
        "bounds_wgs84": [round(value, 8) for value in bounds],
        "boundary_padding_m": BOUNDARY_PADDING_M,
        "source_node_count": source_node_count,
        "location_index": "sparse_file_array (temporary, removed after import)",
        "source_highway_way_count": handler.highway_way_count,
        "car_accessible_way_count": handler.car_way_count,
        "in_scope_way_count": handler.in_scope_way_count,
        "car_highway_values": dict(sorted(handler.car_highway_values.items())),
        "in_scope_highway_values": dict(sorted(handler.in_scope_highway_values.items())),
        "speed_source_values": dict(sorted(handler.speed_source_values.items())),
        "graph_node_count": len(network.nodes),
        "directed_edge_count": len(network.edges),
        "physical_segment_count": len({edge.physical_segment_id for edge in network.edges.values()}),
        "turn_restriction_count": len(network.turn_restrictions),
        "unsupported_turn_restriction_count": handler.unsupported_turn_restriction_count,
        "segments_excluded_at_region_boundary": handler.missing_node_segments,
        "access_rejections": dict(sorted(handler.access_rejections.items())),
        "unrecognized_access_values": dict(sorted(handler.access_unrecognized.items())),
        "malformed_or_non_numeric_maxspeed_count": handler.malformed_speed_count,
        "excluded_unrecognized_oneway_values": dict(
            sorted(handler.unrecognized_oneway_values.items())
        ),
        "speed_defaults_kph": DEFAULT_SPEED_KPH,
        "access_precedence": list(ACCESS_PRECEDENCE),
        "excluded_access_values": sorted(RESTRICTED_ACCESS),
        "unrecognized_access_value_policy": (
            "Explicit unrecognized values are excluded from the passenger-car graph."
        ),
        "unsupported_features": [
            "conditional access and conditional speed tags",
            "conditional turn restrictions",
            "via-way turn restrictions",
            "barrier nodes",
            "ferry routes",
            "one-way and access rules on relations other than supported via-node restrictions",
            "ways with unrecognized one-way values",
        ],
    }
    return network, report


def build_and_persist() -> dict[str, object]:
    raw_dir = PROJECT_ROOT / "data/raw"
    pbf_path = raw_dir / "osm/brazil-261007.osm.pbf"
    boundary_manifest_path = PROJECT_ROOT / "data/manifests/ibge-boundary-mt.json"
    boundary_manifest = json.loads(boundary_manifest_path.read_text(encoding="utf-8"))
    boundary_path = PROJECT_ROOT / boundary_manifest["source_path"]
    pbf_manifest = json.loads(
        (PROJECT_ROOT / "data/manifests/osm-brazil-261007.json").read_text(encoding="utf-8")
    )
    network, quality = build_road_network(
        pbf_path,
        boundary_path,
        expected_pbf_sha256=pbf_manifest["sha256"],
        expected_boundary_sha256=boundary_manifest["sha256"],
    )
    graph_path = PROJECT_ROOT / "data/processed/road-network-mt.jsonl.gz"
    network.to_jsonl_gz(graph_path)
    graph_sha256 = file_sha256(graph_path)
    quality_path = PROJECT_ROOT / "data/processed/road-network-quality.json"
    quality_path.parent.mkdir(parents=True, exist_ok=True)
    quality_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "dataset": "OpenStreetMap passenger-car network for Mato Grosso study",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_pbf": {
            "source_url": pbf_manifest["source_url"],
            "data_timestamp_utc": pbf_manifest["source_data_timestamp_utc"],
            "path": pbf_manifest["source_path"],
            "sha256": pbf_manifest["sha256"],
            "license": pbf_manifest["license"],
            "attribution": pbf_manifest["attribution"],
            "attribution_url": pbf_manifest["attribution_url"],
        },
        "boundary_source": {
            "source_url": boundary_manifest["source_url"],
            "path": boundary_manifest["source_path"],
            "bytes": boundary_path.stat().st_size,
            "sha256": boundary_manifest["sha256"],
            "product": boundary_manifest["source_product"],
            "source_crs": boundary_manifest["source_crs"],
            "license_note": boundary_manifest["license_note"],
        },
        "graph_path": "data/processed/road-network-mt.jsonl.gz",
        "graph_sha256": graph_sha256,
        "quality_report_path": "data/processed/road-network-quality.json",
        "quality": quality,
        "cost_model": "geodesic WGS84 segment length; maxspeed when numeric, otherwise class default",
        "scope_note": (
            "Graph includes routable road segments with both endpoint nodes inside the "
            "IBGE state bounding box padded by 100 km in EPSG:5880. Roads are clipped at "
            "the padded bounds; route costs exclude off-road seat snap connectors."
        ),
    }
    manifest_path = PROJECT_ROOT / "data/manifests/road-network-mt.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"manifest": str(manifest_path.relative_to(PROJECT_ROOT)), **quality}


def main() -> None:
    enforce_memory_limit(
        command=[sys.executable, "-m", "app.graph.builder", *sys.argv[1:]]
    )
    print(json.dumps(build_and_persist(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
