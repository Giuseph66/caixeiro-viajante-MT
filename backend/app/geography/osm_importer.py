from __future__ import annotations

import resource
from collections import Counter
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

import osmium


class RoadWaySummary:
    """Counts raw highway-tagged ways without constructing a routing graph."""

    def __init__(self) -> None:
        self.highway_way_count = 0
        self.highway_values: Counter[str] = Counter()
        self.access_values: Counter[str] = Counter()
        self.vehicle_access_values: Counter[str] = Counter()
        self.motor_vehicle_access_values: Counter[str] = Counter()
        self.oneway_values: Counter[str] = Counter()

    def add_way_tags(self, tags: Mapping[str, str]) -> None:
        highway = tags.get("highway")
        if highway is None:
            return
        self.highway_way_count += 1
        self.highway_values[highway] += 1
        for key, counter in (
            ("access", self.access_values),
            ("vehicle", self.vehicle_access_values),
            ("motor_vehicle", self.motor_vehicle_access_values),
            ("oneway", self.oneway_values),
        ):
            value = tags.get(key)
            if value is not None:
                counter[value] += 1

    def as_dict(self) -> dict[str, object]:
        return {
            "highway_way_count": self.highway_way_count,
            "highway_values": dict(sorted(self.highway_values.items())),
            "low_frequency_highway_values_at_most_4": {
                key: count for key, count in sorted(self.highway_values.items()) if count <= 4
            },
            "access_values": dict(sorted(self.access_values.items())),
            "vehicle_access_values": dict(sorted(self.vehicle_access_values.items())),
            "motor_vehicle_access_values": dict(
                sorted(self.motor_vehicle_access_values.items())
            ),
            "oneway_values": dict(sorted(self.oneway_values.items())),
        }


class _RoadWayHandler(osmium.SimpleHandler):
    def __init__(self) -> None:
        super().__init__()
        self.summary = RoadWaySummary()

    def way(self, way: osmium.osm.Way) -> None:
        self.summary.add_way_tags(way.tags)


def summarize_osm_pbf(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(path)

    handler = _RoadWayHandler()
    started_at = perf_counter()
    handler.apply_file(str(path), locations=False)
    elapsed_seconds = perf_counter() - started_at
    peak_rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    return {
        "file_bytes": path.stat().st_size,
        "parser": "PyOsmium",
        "parser_version": version("osmium"),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "peak_rss_mib": round(peak_rss_mib, 2),
        **handler.summary.as_dict(),
        "scope_note": (
            "Counts OSM ways tagged highway; does not filter vehicle access, "
            "build nodes/arcs, or validate routing connectivity."
        ),
    }
