"""Extrai as rodovias de Mato Grosso da Base Cartográfica Contínua 1:250.000 (IBGE).

A camada serve só de fundo visual no mapa. As rotas são calculadas sobre o grafo
OpenStreetMap; os trechos do IBGE não compartilham identificadores com ele.

Uso: python -m app.geography.ibge_roads [--gpkg-zip CAMINHO]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import struct
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.geography.import_data import file_sha256, write_json

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BC250_URL = (
    "https://geoftp.ibge.gov.br/cartas_e_mapas/bases_cartograficas_continuas/bc250/"
    "versao2025/geopackage/bc250_gpkg_2026-03-03.zip"
)
DEFAULT_ZIP = PROJECT_ROOT / "data/raw/ibge/bc250/bc250_gpkg_2026-03-03.zip"
BOUNDARY_PATH = PROJECT_ROOT / "frontend/public/mt-boundary.geojson"
OUTPUT_PATH = PROJECT_ROOT / "frontend/public/ibge-rodovias-mt.geojson"
MANIFEST_PATH = PROJECT_ROOT / "data/manifests/ibge-roads-mt.json"
ROAD_TABLE = "rod_trecho_rodoviario_l"
COORDINATE_DECIMALS = 4  # ~11 m: suficiente para um fundo de mapa em escala estadual.
SIMPLIFY_TOLERANCE_DEGREES = 0.0004


def parse_gpkg_linework(blob: bytes) -> list[list[tuple[float, float]]]:
    """Decodifica o cabeçalho GeoPackage e o WKB de (Multi)LineString."""
    if blob[:2] != b"GP":
        raise ValueError("Geometria não é GeoPackage binário")
    flags = blob[3]
    envelope_sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    offset = 8 + envelope_sizes[(flags >> 1) & 0b111]
    lines: list[list[tuple[float, float]]] = []
    _read_wkb(blob, offset, lines)
    return lines


def _read_wkb(blob: bytes, offset: int, lines: list[list[tuple[float, float]]]) -> int:
    endian = "<" if blob[offset] == 1 else ">"
    (geometry_type,) = struct.unpack_from(f"{endian}I", blob, offset + 1)
    offset += 5
    # ISO WKB: 1000 = Z, 2000 = M, 3000 = ZM.
    base_type, dimensions = geometry_type % 1000, {0: 2, 1: 3, 2: 3, 3: 4}[geometry_type // 1000]
    if base_type == 2:  # LineString
        (count,) = struct.unpack_from(f"{endian}I", blob, offset)
        offset += 4
        points = []
        for _ in range(count):
            longitude, latitude = struct.unpack_from(f"{endian}dd", blob, offset)
            points.append((longitude, latitude))
            offset += 8 * dimensions
        lines.append(points)
        return offset
    if base_type == 5:  # MultiLineString
        (count,) = struct.unpack_from(f"{endian}I", blob, offset)
        offset += 4
        for _ in range(count):
            offset = _read_wkb(blob, offset, lines)
        return offset
    raise ValueError(f"Tipo WKB inesperado em rodovias: {geometry_type}")


def simplify(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Douglas-Peucker iterativo."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        (x1, y1), (x2, y2) = points[start], points[end]
        dx, dy = x2 - x1, y2 - y1
        length_squared = dx * dx + dy * dy
        farthest, farthest_distance = -1, tolerance
        for index in range(start + 1, end):
            px, py = points[index]
            if length_squared == 0:
                distance = ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
            else:
                ratio = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_squared))
                distance = ((px - x1 - ratio * dx) ** 2 + (py - y1 - ratio * dy) ** 2) ** 0.5
            if distance > farthest_distance:
                farthest, farthest_distance = index, distance
        if farthest != -1:
            keep[farthest] = True
            stack.extend(((start, farthest), (farthest, end)))
    return [point for point, kept in zip(points, keep, strict=True) if kept]


def _load_boundary(path: Path) -> tuple[list[list[list[tuple[float, float]]]], tuple[float, float, float, float]]:
    """Polígonos do estado como lista de [anel externo, buracos...] e bbox."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    polygons: list[list[list[tuple[float, float]]]] = []
    for feature in payload["features"]:
        geometry = feature["geometry"]
        parts = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        for part in parts:
            polygons.append([[(x, y) for x, y, *_ in ring] for ring in part])
    xs = [x for polygon in polygons for x, _ in polygon[0]]
    ys = [y for polygon in polygons for _, y in polygon[0]]
    return polygons, (min(xs), min(ys), max(xs), max(ys))


def _in_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    x1, y1 = ring[-1]
    for x2, y2 in ring:
        if (y2 > y) != (y1 > y) and x < (x1 - x2) * (y - y2) / (y1 - y2) + x2:
            inside = not inside
        x1, y1 = x2, y2
    return inside


def _in_state(x: float, y: float, polygons: list[list[list[tuple[float, float]]]]) -> bool:
    return any(
        _in_ring(x, y, polygon[0]) and not any(_in_ring(x, y, hole) for hole in polygon[1:])
        for polygon in polygons
    )


def _category(jurisdiction: str | None, administration: str | None) -> str:
    values = {jurisdiction, administration}
    if "Federal" in values:
        return "federal"
    if "Estadual/Distrital" in values:
        return "estadual"
    return "outra"


def _iter_state_segments(
    connection: sqlite3.Connection, bbox: tuple[float, float, float, float]
) -> Iterator[sqlite3.Row]:
    west, south, east, north = bbox
    # O índice espacial R-tree do GeoPackage evita varrer o país inteiro.
    yield from connection.execute(
        f"""
        SELECT t.geom, t.jurisdicao, t.administracao, t.revestimento, t.sigla,
               t.tipovia, t.situacaofisica
        FROM {ROAD_TABLE} AS t
        JOIN rtree_{ROAD_TABLE}_geom AS r ON r.id = t.fid
        WHERE r.maxx >= ? AND r.minx <= ? AND r.maxy >= ? AND r.miny <= ?
        """,
        (west, east, south, north),
    )


def build_geojson(gpkg_path: Path, boundary_path: Path = BOUNDARY_PATH) -> tuple[dict[str, Any], dict[str, int]]:
    polygons, bbox = _load_boundary(boundary_path)
    connection = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    features: list[dict[str, Any]] = []
    counts = {"candidates": 0, "outside_state": 0, "kept": 0}
    try:
        for row in _iter_state_segments(connection, bbox):
            counts["candidates"] += 1
            lines = [
                simplify(line, SIMPLIFY_TOLERANCE_DEGREES)
                for line in parse_gpkg_linework(row["geom"])
            ]
            # Trecho entra se algum vértice original cair dentro do estado.
            if not any(_in_state(x, y, polygons) for line in lines for x, y in line):
                counts["outside_state"] += 1
                continue
            coordinates = [
                [[round(x, COORDINATE_DECIMALS), round(y, COORDINATE_DECIMALS)] for x, y in line]
                for line in lines
                if len(line) >= 2
            ]
            if not coordinates:
                continue
            properties: dict[str, Any] = {
                "categoria": _category(row["jurisdicao"], row["administracao"]),
                "pavimentada": row["revestimento"] == "Pavimentado",
            }
            if row["sigla"] and row["sigla"].strip():
                properties["sigla"] = row["sigla"].strip()
            features.append(
                {
                    "type": "Feature",
                    "geometry": (
                        {"type": "LineString", "coordinates": coordinates[0]}
                        if len(coordinates) == 1
                        else {"type": "MultiLineString", "coordinates": coordinates}
                    ),
                    "properties": properties,
                }
            )
            counts["kept"] += 1
    finally:
        connection.close()
    return {"type": "FeatureCollection", "features": features}, counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpkg-zip", type=Path, default=DEFAULT_ZIP)
    args = parser.parse_args()
    zip_path: Path = args.gpkg_zip
    with zipfile.ZipFile(zip_path) as archive:
        member = next(name for name in archive.namelist() if name.endswith(".gpkg"))
        gpkg_path = zip_path.with_name(Path(member).name)
        if not gpkg_path.is_file():
            archive.extract(member, zip_path.parent)
    payload, counts = build_geojson(gpkg_path)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_PATH.with_suffix(".geojson.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT_PATH)
    by_category: dict[str, int] = {}
    for feature in payload["features"]:
        category = feature["properties"]["categoria"]
        by_category[category] = by_category.get(category, 0) + 1
    write_json(
        MANIFEST_PATH,
        {
            "dataset": "IBGE BC250 road segments clipped to Mato Grosso",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_url": BC250_URL,
            "source_path": str(zip_path.relative_to(PROJECT_ROOT)),
            "source_sha256": file_sha256(zip_path),
            "source_layer": ROAD_TABLE,
            "source_crs": "EPSG:4674",
            "boundary_path": str(BOUNDARY_PATH.relative_to(PROJECT_ROOT)),
            "output_path": str(OUTPUT_PATH.relative_to(PROJECT_ROOT)),
            "output_sha256": file_sha256(OUTPUT_PATH),
            "output_bytes": OUTPUT_PATH.stat().st_size,
            "segment_counts": {**counts, "by_category": by_category},
            "simplification": {
                "tolerance_degrees": SIMPLIFY_TOLERANCE_DEGREES,
                "coordinate_decimals": COORDINATE_DECIMALS,
            },
            "scope_note": "Visual background only; routes are computed on the OpenStreetMap graph.",
            "license_note": "Public IBGE source file; the download index does not state a separate license.",
        },
    )
    print(f"{counts['kept']} trechos gravados em {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
