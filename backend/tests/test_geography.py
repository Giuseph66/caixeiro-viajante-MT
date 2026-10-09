import pytest

from app.geography.municipalities import GeographicDataError, validate_municipality_catalog
from app.geography.osm_importer import RoadWaySummary


def test_validate_municipality_catalog_uses_unique_ibge_codes() -> None:
    catalog = validate_municipality_catalog(
        [{"id": 5100102, "nome": "Acorizal"}, {"id": 5100201, "nome": "Água Boa"}],
        expected_count=2,
    )

    assert list(catalog) == [5100102, 5100201]
    assert catalog[5100201].name == "Água Boa"


def test_validate_municipality_catalog_rejects_duplicate_codes() -> None:
    records = [{"id": 5100102, "nome": "Acorizal"}, {"id": 5100102, "nome": "Outro nome"}]

    with pytest.raises(GeographicDataError, match="duplicado"):
        validate_municipality_catalog(records, expected_count=2)


def test_validate_municipality_catalog_rejects_codes_outside_mato_grosso() -> None:
    with pytest.raises(GeographicDataError, match="fora de Mato Grosso"):
        validate_municipality_catalog([{"id": 1100015, "nome": "Alta Floresta d'Oeste"}], expected_count=1)


def test_road_way_summary_counts_highway_and_vehicle_tags() -> None:
    summary = RoadWaySummary()
    summary.add_way_tags({"highway": "primary", "motor_vehicle": "yes", "oneway": "-1"})
    summary.add_way_tags({"highway": "primary", "access": "private"})
    summary.add_way_tags({"building": "yes"})

    report = summary.as_dict()
    assert report["highway_way_count"] == 2
    assert report["highway_values"] == {"primary": 2}
    assert report["motor_vehicle_access_values"] == {"yes": 1}
    assert report["oneway_values"] == {"-1": 1}
