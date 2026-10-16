"""Extrai os cursos d'água nomeados de Mato Grosso da BC250 (IBGE).

Camada só visual, usada para mostrar o nome do rio ao passar o mouse no mapa.
Trechos de um mesmo nome são agrupados numa única feição MultiLineString.

Uso: python -m app.geography.ibge_rivers [--gpkg PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.geography.ibge_roads import (
    BC250_URL,
    BOUNDARY_PATH,
    COORDINATE_DECIMALS,
    DEFAULT_ZIP,
    PROJECT_ROOT,
    _in_state,
    _load_boundary,
    parse_gpkg_linework,
    simplify,
)
from app.geography.import_data import file_sha256, write_json

DRAINAGE_TABLE = "hid_trecho_drenagem_l"
OUTPUT_PATH = PROJECT_ROOT / "frontend/public/ibge-rios-mt.geojson"
MANIFEST_PATH = PROJECT_ROOT / "data/manifests/ibge-rivers-mt.json"
SIMPLIFY_TOLERANCE_DEGREES = 0.0008  # ~90 m; rios só precisam de traçado reconhecível.


def build_geojson(
    gpkg_path: Path, boundary_path: Path = BOUNDARY_PATH
) -> tuple[dict[str, Any], dict[str, int]]:
    polygons, (west, south, east, north) = _load_boundary(boundary_path)
    connection = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
    lines_by_name: dict[str, list[list[list[float]]]] = {}
    permanent_by_name: dict[str, bool] = {}
    counts = {"candidates": 0, "outside_state": 0, "kept_segments": 0}
    try:
        rows = connection.execute(
            f"""
            SELECT t.geom, t.nome, t.regime
            FROM {DRAINAGE_TABLE} AS t
            JOIN rtree_{DRAINAGE_TABLE}_geom AS r ON r.id = t.fid
            WHERE r.maxx >= ? AND r.minx <= ? AND r.maxy >= ? AND r.miny <= ?
              AND t.nome IS NOT NULL AND trim(t.nome) <> ''
            """,
            (west, east, south, north),
        )
        for geom, name, regime in rows:
            counts["candidates"] += 1
            lines = parse_gpkg_linework(geom)
            if not any(_in_state(x, y, polygons) for line in lines for x, y in line):
                counts["outside_state"] += 1
                continue
            key = name.strip()
            for line in lines:
                points = simplify(line, SIMPLIFY_TOLERANCE_DEGREES)
                if len(points) < 2:
                    continue
                lines_by_name.setdefault(key, []).append(
                    [[round(x, COORDINATE_DECIMALS), round(y, COORDINATE_DECIMALS)] for x, y in points]
                )
                counts["kept_segments"] += 1
            permanent_by_name[key] = permanent_by_name.get(key, False) or regime == "Permanente"
    finally:
        connection.close()
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "MultiLineString", "coordinates": lines},
            "properties": {"nome": name, "permanente": permanent_by_name[name]},
        }
        for name, lines in sorted(lines_by_name.items())
    ]
    counts["rivers"] = len(features)
    return {"type": "FeatureCollection", "features": features}, counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gpkg",
        type=Path,
        default=DEFAULT_ZIP.with_name("bc250_2026-03-03.gpkg"),
        help="GeoPackage extraído; gerado por `python -m app.geography.ibge_roads`",
    )
    args = parser.parse_args()
    payload, counts = build_geojson(args.gpkg)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_PATH.with_suffix(".geojson.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT_PATH)
    write_json(
        MANIFEST_PATH,
        {
            "dataset": "IBGE BC250 named watercourses clipped to Mato Grosso",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_url": BC250_URL,
            "source_path": str(DEFAULT_ZIP.relative_to(PROJECT_ROOT)),
            "source_sha256": file_sha256(DEFAULT_ZIP),
            "source_layer": DRAINAGE_TABLE,
            "source_crs": "EPSG:4674",
            "boundary_path": str(BOUNDARY_PATH.relative_to(PROJECT_ROOT)),
            "output_path": str(OUTPUT_PATH.relative_to(PROJECT_ROOT)),
            "output_sha256": file_sha256(OUTPUT_PATH),
            "output_bytes": OUTPUT_PATH.stat().st_size,
            "counts": counts,
            "simplification": {
                "tolerance_degrees": SIMPLIFY_TOLERANCE_DEGREES,
                "coordinate_decimals": COORDINATE_DECIMALS,
            },
            "scope_note": "Visual layer only; unnamed watercourses are omitted.",
            "license_note": "Public IBGE source file; the download index does not state a separate license.",
        },
    )
    print(f"{counts['rivers']} rios ({counts['kept_segments']} trechos) em {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
