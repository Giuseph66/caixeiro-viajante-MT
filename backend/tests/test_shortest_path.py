from __future__ import annotations

from app.graph.network import RoadEdge, RoadNetwork, RoadNode, TurnRestriction
from app.shortest_path.search import astar, dijkstra


def _edge(
    edge_id: str,
    source: int,
    target: int,
    way_id: int,
    length_m: float,
    travel_time_s: float,
) -> RoadEdge:
    return RoadEdge(
        edge_id,
        source,
        target,
        way_id,
        edge_id,
        length_m,
        travel_time_s,
        "primary",
        length_m / travel_time_s * 3.6,
        "test",
    )


def _network() -> RoadNetwork:
    nodes = {
        1: RoadNode(1, -56.0, -15.0),
        2: RoadNode(2, -55.9999072, -15.0),
        3: RoadNode(3, -55.9998144, -15.0),
        4: RoadNode(4, -56.0, -14.99973),
    }
    edges = {
        "short-a": _edge("short-a", 1, 2, 10, 10.0, 10.0),
        "short-b": _edge("short-b", 2, 3, 11, 10.0, 10.0),
        "fast-a": _edge("fast-a", 1, 4, 20, 35.0, 1.0),
        "fast-b": _edge("fast-b", 4, 3, 21, 40.0, 1.0),
    }
    return RoadNetwork(nodes, edges)


def test_astar_matches_dijkstra_and_recovers_directed_edges() -> None:
    graph = _network()
    expected = dijkstra(graph, 1, 3, "distance")
    actual = astar(graph, 1, 3, "distance")

    assert expected is not None and actual is not None
    assert actual.cost == expected.cost == 20.0
    assert actual.edge_ids == ("short-a", "short-b")
    assert actual.node_ids == (1, 2, 3)


def test_distance_and_time_use_independent_costs() -> None:
    graph = _network()

    distance = dijkstra(graph, 1, 3, "distance")
    travel_time = dijkstra(graph, 1, 3, "time")

    assert distance is not None and distance.edge_ids == ("short-a", "short-b")
    assert travel_time is not None and travel_time.edge_ids == ("fast-a", "fast-b")
    assert astar(graph, 1, 3, "time") == travel_time


def test_one_way_and_turn_restriction_make_route_unreachable() -> None:
    nodes = {
        1: RoadNode(1, -56.0, -15.0),
        2: RoadNode(2, -55.99995, -15.0),
        3: RoadNode(3, -55.9999, -15.0),
        4: RoadNode(4, -55.99995, -14.99995),
    }
    edges = {
        "first": _edge("first", 1, 2, 10, 10.0, 1.0),
        "blocked": _edge("blocked", 2, 3, 20, 10.0, 1.0),
        "alternate-a": _edge("alternate-a", 2, 4, 30, 10.0, 1.0),
        "alternate-b": _edge("alternate-b", 4, 3, 40, 10.0, 1.0),
    }
    graph = RoadNetwork(
        nodes,
        edges,
        (TurnRestriction(from_way_id=10, via_node_id=2, to_way_id=20, kind="no"),),
    )

    assert dijkstra(graph, 3, 1, "distance") is None
    route = dijkstra(graph, 1, 3, "distance")
    assert route is not None
    assert route.edge_ids == ("first", "alternate-a", "alternate-b")


def test_same_node_path_has_zero_cost_and_no_edges() -> None:
    result = dijkstra(_network(), 1, 1)

    assert result is not None
    assert result.cost == 0.0
    assert result.node_ids == (1,)
    assert result.edge_ids == ()
