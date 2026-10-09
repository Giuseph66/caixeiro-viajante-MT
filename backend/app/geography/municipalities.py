from __future__ import annotations

import json
import math
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import shapefile
from pyproj import CRS, Transformer

EXPECTED_MUNICIPALITIES = 142
MATO_GROSSO_UF_CODE = "51"
SEAT_CATEGORY = "Cidade"
SEAT_SUBCATEGORY = "Sede Municipal"


class GeographicDataError(ValueError):
    """Raised when an official geographic snapshot fails validation."""


@dataclass(frozen=True)
class Municipality:
    ibge_code: int
    name: str


def validate_municipality_catalog(
    records: list[dict[str, object]], expected_count: int = EXPECTED_MUNICIPALITIES
) -> dict[int, Municipality]:
    municipalities: dict[int, Municipality] = {}
    for record in records:
        try:
            code = int(record["id"])
            name = str(record["nome"]).strip()
        except (KeyError, TypeError, ValueError) as error:
            raise GeographicDataError("Registro IBGE sem código ou nome válido") from error

        if not str(code).startswith(MATO_GROSSO_UF_CODE) or not name:
            raise GeographicDataError(f"Registro fora de Mato Grosso ou sem nome: {record!r}")
        if code in municipalities:
            raise GeographicDataError(f"Código IBGE duplicado: {code}")
        municipalities[code] = Municipality(ibge_code=code, name=name)

    if len(municipalities) != expected_count:
        raise GeographicDataError(
            f"Esperados {expected_count} municípios de Mato Grosso; encontrados {len(municipalities)}"
        )
    return dict(sorted(municipalities.items()))


def load_municipality_catalog(
    json_path: Path, expected_count: int = EXPECTED_MUNICIPALITIES
) -> dict[int, Municipality]:
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GeographicDataError(f"Não foi possível ler o snapshot IBGE: {json_path}") from error
    if not isinstance(payload, list):
        raise GeographicDataError("O snapshot IBGE deve conter uma lista de municípios")
    return validate_municipality_catalog(payload, expected_count=expected_count)


def load_municipal_seats(
    shapefile_zip: Path,
    municipalities: dict[int, Municipality],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    with ZipFile(shapefile_zip) as archive:
        shape_paths = [
            name
            for name in archive.namelist()
            if name.upper().startswith("MT/") and name.lower().endswith("_localidades_2022.shp")
        ]
        if len(shape_paths) != 1:
            raise GeographicDataError(
                f"Esperado um shapefile de localidades para MT; encontrados {len(shape_paths)}"
            )

        shape_path = shape_paths[0]
        shape_base = shape_path[:-4]
        members = {name.lower(): name for name in archive.namelist()}

        def member_bytes(extension: str) -> bytes:
            key = f"{shape_base}.{extension}".lower()
            try:
                return archive.read(members[key])
            except KeyError as error:
                raise GeographicDataError(f"Arquivo {extension} ausente no pacote IBGE") from error

        source_crs = CRS.from_wkt(member_bytes("prj").decode("utf-8")).to_epsg()
        if source_crs != 4674:
            raise GeographicDataError(f"CRS do shapefile mudou; esperado EPSG:4674, recebido {source_crs}")

        reader = shapefile.Reader(
            shp=BytesIO(member_bytes("shp")),
            shx=BytesIO(member_bytes("shx")),
            dbf=BytesIO(member_bytes("dbf")),
            encoding="utf-8",
        )
        fields = [field[0] for field in reader.fields[1:]]
        required_fields = {
            "CD_UF",
            "CD_MUN",
            "NM_MUN",
            "CT_LOCALID",
            "SCT_LOCALI",
            "CD_LOCALID",
            "NM_LOCALID",
        }
        missing_fields = sorted(required_fields - set(fields))
        if missing_fields:
            raise GeographicDataError(f"Campos necessários ausentes no shapefile: {missing_fields}")

        transform = Transformer.from_crs(source_crs, 4326, always_xy=True)
        features: list[dict[str, object]] = []
        counts_by_code: dict[int, int] = {}
        name_mismatches: list[int] = []
        locality_code_mismatches: list[int] = []
        max_attribute_delta = 0.0
        invalid_coordinates: list[int] = []

        for shape_record in reader.iterShapeRecords():
            row = dict(zip(fields, shape_record.record, strict=True))
            if (
                str(row["CD_UF"]) != MATO_GROSSO_UF_CODE
                or row["CT_LOCALID"] != SEAT_CATEGORY
                or row["SCT_LOCALI"] != SEAT_SUBCATEGORY
            ):
                continue

            code = int(row["CD_MUN"])
            municipality = municipalities.get(code)
            if municipality is None:
                raise GeographicDataError(f"Sede do código desconhecido {code} encontrada no IBGE")
            counts_by_code[code] = counts_by_code.get(code, 0) + 1
            if row["NM_MUN"] != municipality.name or row["NM_LOCALID"] != municipality.name:
                name_mismatches.append(code)
            if int(row["CD_LOCALID"]) != code:
                locality_code_mismatches.append(code)

            points = shape_record.shape.points
            if len(points) != 1:
                raise GeographicDataError(f"Sede {code} não possui exatamente um ponto")
            source_lon, source_lat = points[0]
            if not (math.isfinite(source_lon) and math.isfinite(source_lat)):
                invalid_coordinates.append(code)
                continue
            lon, lat = transform.transform(source_lon, source_lat)
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                invalid_coordinates.append(code)
                continue

            attribute_delta = max(
                abs(source_lon - float(row["LONG_LOCAL"])),
                abs(source_lat - float(row["LAT_LOCALI"])),
            )
            max_attribute_delta = max(max_attribute_delta, attribute_delta)
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "ibge_code": code,
                        "municipality_name": municipality.name,
                        "seat_locality_name": str(row["NM_LOCALID"]),
                        "locality_code": int(row["CD_LOCALID"]),
                        "source_category": str(row["CT_LOCALID"]),
                        "source_subcategory": str(row["SCT_LOCALI"]),
                        "source_latitude": source_lat,
                        "source_longitude": source_lon,
                    },
                }
            )

    code_set = set(municipalities)
    duplicate_codes = sorted(code for code, count in counts_by_code.items() if count != 1)
    missing_codes = sorted(code_set - set(counts_by_code))
    if len(features) != len(municipalities) or missing_codes or duplicate_codes:
        raise GeographicDataError(
            "As sedes não cobrem exatamente uma vez os municípios; "
            f"features={len(features)}, ausentes={missing_codes}, duplicados={duplicate_codes}"
        )
    if name_mismatches or locality_code_mismatches or invalid_coordinates:
        raise GeographicDataError(
            "Inconsistência nas sedes: "
            f"nomes={sorted(set(name_mismatches))}, "
            f"códigos de localidade={sorted(set(locality_code_mismatches))}, "
            f"coordenadas inválidas={sorted(set(invalid_coordinates))}"
        )

    return features, {
        "municipality_count": len(municipalities),
        "seat_feature_count": len(features),
        "unique_municipality_codes": len(counts_by_code),
        "missing_municipality_codes": [],
        "duplicate_municipality_codes": [],
        "municipality_name_mismatches": [],
        "locality_code_mismatches": [],
        "invalid_coordinates": [],
        "source_crs": f"EPSG:{source_crs}",
        "output_crs": "EPSG:4326",
        "max_source_attribute_coordinate_delta_degrees": max_attribute_delta,
        "coordinate_source": "shapefile point geometry; DBF coordinate columns are rounded",
    }


def write_seats_geojson(path: Path, features: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    collection = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(collection, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
