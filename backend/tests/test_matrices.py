from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

import app.routing.matrices as matrices_module
from app.graph.network import RoadEdge, RoadNetwork, RoadNode
from app.routing.matrices import (
    SeatSnap,
    _validate_manifested_cache_digest,
    build_metric_matrix,
    matrix_cache_key,
    snap_seats,
)


def test_matrix_keeps_directed_costs_and_edge_paths() -> None:
    nodes = {
        1: RoadNode(1, -56.0, -15.0),
        2: RoadNode(2, -55.999, -15.0),
    }
    edges = {
        "one-way": RoadEdge(
            "one-way",
            1,
            2,
            7,
            "physical",
            123.0,
            12.0,
            "primary",
            36.9,
            "test",
        )
    }
    graph = RoadNetwork(nodes, edges)
    snaps = [
        SeatSnap(5100102, "Acorizal", -56.0, -15.0, 1, 0.0, True),
        SeatSnap(5100201, "Água Boa", -55.999, -15.0, 2, 0.0, True),
    ]

    matrix = build_metric_matrix(graph, snaps, "distance")

    assert matrix.costs == [[0.0, 123.0], [None, 0.0]]
    assert matrix.paths == [[[], ["one-way"]], [None, []]]
    assert matrix.unreachable_directed_pairs == 1


def test_completed_matrix_rows_resume_from_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = RoadNetwork(
        {
            1: RoadNode(1, -56.0, -15.0),
            2: RoadNode(2, -55.999, -15.0),
        },
        {
            "one-way": RoadEdge(
                "one-way", 1, 2, 7, "physical", 123.0, 12.0, "primary", 36.9, "test"
            )
        },
    )
    snaps = [
        SeatSnap(5100102, "Acorizal", -56.0, -15.0, 1, 0.0, True),
        SeatSnap(5100201, "Água Boa", -55.999, -15.0, 2, 0.0, True),
    ]
    computed = build_metric_matrix(
        graph,
        snaps,
        "distance",
        checkpoint_dir=tmp_path / "distance",
        checkpoint_key="test-cache-key",
    )

    def unexpected_search(*args: object, **kwargs: object) -> object:
        raise AssertionError("linha concluída deveria vir do checkpoint")

    monkeypatch.setattr(matrices_module, "all_reachable_targets", unexpected_search)
    resumed = build_metric_matrix(
        graph,
        snaps,
        "distance",
        checkpoint_dir=tmp_path / "distance",
        checkpoint_key="test-cache-key",
    )

    assert resumed == computed


def test_cache_key_changes_with_graph_and_snap_assignment() -> None:
    snap = SeatSnap(5100102, "Acorizal", -56.0, -15.0, 1, 3.0, True)
    profile = {"primary": 80.0}

    first = matrix_cache_key("graph-a", [snap], profile)
    changed_graph = matrix_cache_key("graph-b", [snap], profile)
    changed_snap = matrix_cache_key(
        "graph-a",
        [SeatSnap(5100102, "Acorizal", -56.0, -15.0, 2, 3.0, True)],
        profile,
    )

    assert first != changed_graph
    assert first != changed_snap


def test_cache_manifest_rejects_changed_cache_bytes(tmp_path: Path) -> None:
    cache_path = tmp_path / "matrix.json.gz"
    manifest_path = tmp_path / "travel-matrix.json"
    original_bytes = b"immutable matrix cache"
    cache_path.write_bytes(original_bytes)
    manifest_path.write_text(
        json.dumps(
            {
                "cache_key": "matrix-key",
                "cache_sha256": sha256(original_bytes).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    cache_path.write_bytes(b"changed matrix cache")

    with pytest.raises(ValueError, match="SHA-256"):
        _validate_manifested_cache_digest(
            cache_path,
            cache_key="matrix-key",
            manifest_path=manifest_path,
        )


def test_142_synthetic_seats_snap_to_nearest_network_node() -> None:
    graph = RoadNetwork(
        {
            1: RoadNode(1, -56.0, -15.0),
            2: RoadNode(2, -55.999, -15.0),
        },
        {
            "forward": RoadEdge(
                "forward",
                1,
                2,
                7,
                "physical",
                100.0,
                10.0,
                "primary",
                36.0,
                "test",
            )
        },
    )
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-56.0, -15.0]},
            "properties": {
                "ibge_code": 5100000 + index,
                "municipality_name": f"Municipio {index}",
            },
        }
        for index in range(1, 143)
    ]

    snaps = snap_seats(features, graph)

    assert len(snaps) == 142
    assert all(snap.node_id == 1 for snap in snaps)
    assert all(snap.snap_distance_m == 0.0 for snap in snaps)
