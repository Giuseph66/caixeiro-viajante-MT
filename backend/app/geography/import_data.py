from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from app.geography.municipalities import (
    load_municipal_seats,
    load_municipality_catalog,
    write_seats_geojson,
)
from app.geography.osm_importer import summarize_osm_pbf

PROJECT_ROOT = Path(__file__).resolve().parents[3]
IBGE_API_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/estados/51/municipios"
IBGE_LOCALITIES_URL = (
    "https://geoftp.ibge.gov.br/organizacao_do_territorio/estrutura_territorial/"
    "localidades/Localidades_do_Brasil/2022/Localidades_UFs_shp.zip"
)
OSM_URL = "https://download.geofabrik.de/south-america/brazil-261007.osm.pbf"


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def import_municipalities() -> dict[str, object]:
    raw_dir = PROJECT_ROOT / "data/raw/ibge"
    processed_dir = PROJECT_ROOT / "data/processed"
    manifest_dir = PROJECT_ROOT / "data/manifests"
    api_path = raw_dir / "municipios-mt.json"
    localities_path = raw_dir / "Localidades_UFs_shp.zip"
    catalog = load_municipality_catalog(api_path)
    features, quality = load_municipal_seats(localities_path, catalog)

    output_path = processed_dir / "municipalities-mt.geojson"
    write_seats_geojson(output_path, features)
    quality_path = processed_dir / "municipality-quality.json"
    write_json(quality_path, quality)

    manifest = {
        "dataset": "Mato Grosso municipalities and municipal seats",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "municipality_catalog": {
            "source_url": IBGE_API_URL,
            "snapshot_path": "data/raw/ibge/municipios-mt.json",
            "snapshot_bytes": api_path.stat().st_size,
            "sha256": file_sha256(api_path),
            "record_count": len(catalog),
        },
        "municipal_seats": {
            "source_url": IBGE_LOCALITIES_URL,
            "source_product": "Censo Demográfico 2022 - Localidades do Brasil",
            "source_path": "data/raw/ibge/Localidades_UFs_shp.zip",
            "source_bytes": localities_path.stat().st_size,
            "source_file_last_modified_utc": "2025-11-19T19:15:51Z",
            "source_sha256": file_sha256(localities_path),
            "category": "Cidade",
            "subcategory": "Sede Municipal",
            "source_crs": quality["source_crs"],
            "output_crs": quality["output_crs"],
            "output_path": "data/processed/municipalities-mt.geojson",
            "output_sha256": file_sha256(output_path),
        },
        "quality_report_path": "data/processed/municipality-quality.json",
        "quality": quality,
        "license_note": (
            "IBGE source page lists these public files; no separate open-data license "
            "was stated on the download index. Preserve source attribution."
        ),
    }
    manifest_path = manifest_dir / "municipalities-mt.json"
    write_json(manifest_path, manifest)
    return {"manifest": str(manifest_path.relative_to(PROJECT_ROOT)), **quality}


def import_osm_network_summary() -> dict[str, object]:
    raw_path = PROJECT_ROOT / "data/raw/osm/brazil-261007.osm.pbf"
    report = summarize_osm_pbf(raw_path)
    report_path = PROJECT_ROOT / "data/processed/osm-quality.json"
    write_json(report_path, report)

    checksum_path = raw_path.with_suffix(raw_path.suffix + ".md5")
    expected_md5 = checksum_path.read_text(encoding="ascii").split()[0]
    manifest = {
        "dataset": "OpenStreetMap Brazil PBF",
        "source_url": OSM_URL,
        "source_data_timestamp_utc": "2026-10-07T20:20:35Z",
        "source_file_last_modified_utc": "2026-10-08T15:32:33Z",
        "source_path": "data/raw/osm/brazil-261007.osm.pbf",
        "downloaded_at_utc": datetime.fromtimestamp(
            raw_path.stat().st_mtime, timezone.utc
        ).isoformat(),
        "file_bytes": raw_path.stat().st_size,
        "md5_geofabrik": expected_md5,
        "sha256": file_sha256(raw_path),
        "license": "Open Database License (ODbL) 1.0",
        "attribution": "© OpenStreetMap contributors",
        "attribution_url": "https://www.openstreetmap.org/copyright",
        "coverage": "Brazil; kept national extract to avoid clipping paths at state boundaries.",
        "data_profile": "All OSM ways carrying a highway tag are counted without access filtering.",
        "parser_report_path": "data/processed/osm-quality.json",
        "parser_report": report,
    }
    manifest_path = PROJECT_ROOT / "data/manifests/osm-brazil-261007.json"
    write_json(manifest_path, manifest)
    return {
        "manifest": str(manifest_path.relative_to(PROJECT_ROOT)),
        "file_bytes": report["file_bytes"],
        "highway_way_count": report["highway_way_count"],
        "elapsed_seconds": report["elapsed_seconds"],
        "peak_rss_mib": report["peak_rss_mib"],
    }


def main() -> None:
    started = time.perf_counter()
    municipality_result = import_municipalities()
    osm_result = import_osm_network_summary()
    print(
        json.dumps(
            {
                "municipalities": municipality_result,
                "osm": osm_result,
                "total_elapsed_seconds": round(time.perf_counter() - started, 3),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
