from __future__ import annotations

from app.graph.network import RoadEdge, RoadNetwork, RoadNode
from app.routing.matrices import (
    SeatSnap,
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
