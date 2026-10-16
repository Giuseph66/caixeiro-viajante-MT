from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import PROJECT_ROOT
from app.core.memory_limit import (
    MAX_MEMORY_BYTES,
    MEMORY_BUDGET_RATIO,
    MINIMUM_MEMORY_BUDGET_BYTES,
    available_memory_bytes,
    memory_budget_bytes,
)
from app.geography.municipalities import load_municipality_catalog
from app.optimization.overlap import OVERLAP_ALGORITHM_VERSION
from app.optimization.visitation.mtsp import MTSP_ALGORITHM_VERSION
from app.optimization.visitation.tsp import TSP_ALGORITHM_VERSION
from app.schemas.api import DatasetResponse, MunicipalityResponse

router = APIRouter(prefix="/api", tags=["dados e configurações"])

DATASET_FILES = {
    "municipalities": ("Municípios e sedes oficiais", "data/manifests/municipalities-mt.json"),
    "road-network": ("Rede rodoviária OpenStreetMap", "data/manifests/road-network-mt.json"),
    "travel-matrix": ("Matrizes dirigidas de distância e tempo", "data/manifests/travel-matrix-mt.json"),
    "open-tsp": ("Resultados open TSP D4", "data/manifests/tsp-benchmarks-mt.json"),
    "open-mtsp": ("Resultados open mTSP D5", "data/manifests/mtsp-benchmarks-mt.json"),
    "overlap": ("Resultados de sobreposição D6", "data/manifests/overlap-benchmarks-mt.json"),
}


@router.get("/resources/memory-budget", tags=["recursos"])
def read_memory_budget() -> dict[str, Any]:
    available = available_memory_bytes()
    budget = memory_budget_bytes(available)
    return {
        "available_memory_bytes": available,
        "budget_bytes": budget,
        "hard_limit_bytes": MAX_MEMORY_BYTES,
        "fraction_of_available": MEMORY_BUDGET_RATIO,
        "minimum_budget_bytes": MINIMUM_MEMORY_BUDGET_BYTES,
        "can_start": budget >= MINIMUM_MEMORY_BUDGET_BYTES,
    }


@router.get("/configurations", tags=["configurações"])
@router.get("/experiments/configurations", tags=["configurações"])
def read_configurations() -> dict[str, Any]:
    return {
        "job_types": [
            {"id": "tsp", "title": "Open TSP (D4)"},
            {"id": "mtsp", "title": "Open mTSP (D5)"},
            {"id": "overlap", "title": "Sobreposição P1/P2/P3 (D6)"},
        ],
        "agent_counts": [1, 2, 3],
        "objectives": ["distance", "time"],
        "origin_policies": ["fixed_cuiaba", "optimized_among_seats"],
        "overlap_policies": ["P1", "P2", "P3"],
        "limits": {
            "time_limit_s": {"minimum": 1, "maximum": 300},
            "workers": {"minimum": 1, "maximum": 8},
            "variants": {"minimum": 1, "maximum": 8},
            "memory_limit_bytes": 25_000_000_000,
        },
        "defaults": {
            "time_limit_s": 60,
            "workers": 8,
            "variants": 8,
            "seed": 20261008,
            "lambdas": [0, 0.25, 0.5, 1, 2],
        },
    }


@router.get("/algorithms", tags=["algoritmos"])
def list_algorithms() -> list[dict[str, str]]:
    return [
        {"id": "tsp", "title": "Open TSP", "version": TSP_ALGORITHM_VERSION},
        {"id": "mtsp", "title": "Open mTSP", "version": MTSP_ALGORITHM_VERSION},
        {"id": "overlap", "title": "Políticas de sobreposição", "version": OVERLAP_ALGORITHM_VERSION},
    ]


@router.get("/municipalities", response_model=list[MunicipalityResponse])
def list_municipalities() -> list[MunicipalityResponse]:
    return list(_municipalities())


@router.get("/municipalities/{ibge_code}", response_model=MunicipalityResponse)
def get_municipality(ibge_code: int) -> MunicipalityResponse:
    municipality = next(
        (item for item in _municipalities() if item.ibge_code == ibge_code), None
    )
    if municipality is None:
        raise HTTPException(status_code=404, detail="Município não encontrado")
    return municipality


@router.get("/datasets", response_model=list[DatasetResponse])
@router.get("/datasets/status", response_model=list[DatasetResponse])
def list_datasets() -> list[DatasetResponse]:
    return [_dataset_response(dataset_id) for dataset_id in DATASET_FILES]


@router.get("/datasets/{dataset_id}", response_model=DatasetResponse)
def get_dataset(dataset_id: str) -> DatasetResponse:
    if dataset_id not in DATASET_FILES:
        raise HTTPException(status_code=404, detail="Dataset não encontrado")
    return _dataset_response(dataset_id)


@router.get("/geojson/municipalities")
def municipalities_geojson() -> FileResponse:
    path = PROJECT_ROOT / "data/processed/municipalities-mt.geojson"
    if not path.is_file():
        raise HTTPException(status_code=503, detail="GeoJSON de sedes não está disponível")
    return FileResponse(path, media_type="application/geo+json")


@lru_cache(maxsize=1)
def _municipalities() -> tuple[MunicipalityResponse, ...]:
    catalog_path = PROJECT_ROOT / "data/raw/ibge/municipios-mt.json"
    seats_path = PROJECT_ROOT / "data/processed/municipalities-mt.geojson"
    catalog = load_municipality_catalog(catalog_path)
    collection = json.loads(seats_path.read_text(encoding="utf-8"))
    coordinates = {
        int(feature["properties"]["ibge_code"]): feature["geometry"]["coordinates"]
        for feature in collection["features"]
    }
    if set(coordinates) != set(catalog):
        raise RuntimeError("GeoJSON de sedes diverge do catálogo oficial de municípios")
    return tuple(
        MunicipalityResponse(
            ibge_code=code,
            name=municipality.name,
            longitude=float(coordinates[code][0]),
            latitude=float(coordinates[code][1]),
        )
        for code, municipality in catalog.items()
    )


@lru_cache(maxsize=len(DATASET_FILES))
def _dataset_response(dataset_id: str) -> DatasetResponse:
    try:
        title, relative_path = DATASET_FILES[dataset_id]
    except KeyError as error:
        raise ValueError(f"Dataset não permitido: {dataset_id}") from error
    path = PROJECT_ROOT / relative_path
    if not path.is_file():
        return DatasetResponse(
            dataset_id=dataset_id,
            title=title,
            manifest_path=relative_path,
            manifest_sha256="",
            available=False,
            details={},
        )

    file_size = path.stat().st_size
    digest_builder = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest_builder.update(chunk)
    digest = digest_builder.hexdigest()
    details: dict[str, Any] = {"file_bytes": file_size}
    if file_size < 1_000_000:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key in (
            "dataset",
            "created_at_utc",
            "municipality_count",
            "record_count",
            "cache_sha256",
            "graph_sha256",
            "quality",
        ):
            if key in payload:
                details[key] = payload[key]
    else:
        details["report"] = "JSON de resultados; resumo disponível após a leitura do job"
    return DatasetResponse(
        dataset_id=dataset_id,
        title=title,
        manifest_path=relative_path,
        manifest_sha256=digest,
        available=True,
        details=details,
    )
