from __future__ import annotations

from pathlib import Path

from app.graph.builder import _directions, _speed_for_way, is_car_accessible
from app.graph.network import RoadEdge, RoadNetwork, RoadNode, TurnRestriction


def _edge(
    edge_id: str,
    source: int,
    target: int,
    way_id: int,
    physical_id: str | None = None,
    *,
    length_m: float = 100.0,
    travel_time_s: float = 10.0,
    speed_kph: float = 36.0,
) -> RoadEdge:
    return RoadEdge(
        edge_id=edge_id,
        source=source,
        target=target,
        way_id=way_id,
        physical_segment_id=physical_id or edge_id,
        length_m=length_m,
        travel_time_s=travel_time_s,
        highway="residential",
        speed_kph=speed_kph,
        speed_source="test",
    )


def test_car_access_precedence_and_road_class_filter() -> None:
    assert is_car_accessible(
        {"highway": "residential", "access": "private", "motorcar": "yes"}
    ) == (True, None)
    assert is_car_accessible({"highway": "service", "motor_vehicle": "no"}) == (
        False,
        "no",
    )
    assert is_car_accessible({"highway": "primary", "motor_vehicle": "não"}) == (
        False,
        "no",
    )
    assert is_car_accessible({"highway": "footway"}) == (False, None)


def test_oneway_direction_and_roundabout_defaults() -> None:
    assert _directions({"oneway": "yes"}) == (True, False, False)
    assert _directions({"oneway": "-1"}) == (False, True, False)
    assert _directions({"junction": "roundabout"}) == (True, False, False)
    assert _directions({"junction": "roundabout", "oneway": "no"}) == (
        True,
        True,
        False,
    )


def test_speed_uses_numeric_maxspeed_or_documented_class_default() -> None:
    assert _speed_for_way({"maxspeed": "60 mph"}, "primary") == (
        96.56064,
        "maxspeed",
        False,
    )
    assert _speed_for_way({"maxspeed": "signals"}, "primary") == (
        80.0,
        "class_default",
        True,
    )


def test_physical_segment_shared_by_reverse_edges_and_graph_round_trip(tmp_path: Path) -> None:
    nodes = {
        1: RoadNode(1, -56.0, -15.0),
        2: RoadNode(2, -55.999, -15.0),
    }
    forward = _edge("segment:f", 1, 2, 7, "segment")
    reverse = _edge("segment:r", 2, 1, 7, "segment")
    graph = RoadNetwork(nodes, {forward.edge_id: forward, reverse.edge_id: reverse})
    path = tmp_path / "road-network.jsonl.gz"

    graph.to_jsonl_gz(path)
    loaded = RoadNetwork.from_jsonl_gz(path)

    assert set(loaded.edges) == {"segment:f", "segment:r"}
    assert loaded.edges["segment:f"].physical_segment_id == loaded.edges[
        "segment:r"
    ].physical_segment_id
    assert len(loaded.adjacency[1]) == 1


def test_parallel_ways_preserve_independent_physical_segments() -> None:
    nodes = {
        1: RoadNode(1, -56.0, -15.0),
        2: RoadNode(2, -55.999, -15.0),
    }
    edges = {
        "way-1": _edge("way-1", 1, 2, 101, "segment-101"),
        "way-2": _edge("way-2", 1, 2, 202, "segment-202"),
    }

    graph = RoadNetwork(nodes, edges)

    assert len(graph.adjacency[1]) == 2
    assert {
        edge.physical_segment_id for edge in graph.adjacency[1]
    } == {"segment-101", "segment-202"}


def test_via_node_turn_restriction_blocks_only_prohibited_transition() -> None:
    nodes = {node_id: RoadNode(node_id, -56 + node_id * 0.001, -15.0) for node_id in range(1, 5)}
    edges = {
        "in": _edge("in", 1, 2, 11),
        "blocked": _edge("blocked", 2, 3, 22),
        "allowed": _edge("allowed", 2, 4, 33),
    }
    graph = RoadNetwork(
        nodes,
        edges,
        (TurnRestriction(from_way_id=11, via_node_id=2, to_way_id=22, kind="no"),),
    )

    assert not graph.allows_turn(11, 2, 22)
    assert graph.allows_turn(11, 2, 33)
    assert graph.allows_turn(None, 2, 22)
