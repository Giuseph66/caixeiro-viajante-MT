from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Literal

from pyproj import Geod

from app.graph.network import RoadEdge, RoadNetwork

PathMetric = Literal["distance", "time"]
GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True, slots=True)
class RoadPath:
    metric: PathMetric
    cost: float
    node_ids: tuple[int, ...]
    edge_ids: tuple[str, ...]


def _edge_cost(edge: RoadEdge, metric: PathMetric) -> float:
    if metric == "distance":
        return edge.length_m
    if metric == "time":
        return edge.travel_time_s
    raise ValueError(f"Métrica desconhecida: {metric}")


def _heuristic(network: RoadNetwork, node_id: int, goal_id: int, metric: PathMetric) -> float:
    if metric == "distance":
        maximum_speed_mps = 1.0
    else:
        maximum_speed_mps = network.max_speed_kph / 3.6
        if maximum_speed_mps <= 0:
            return 0.0
    node = network.nodes[node_id]
    goal = network.nodes[goal_id]
    _, _, geodesic_m = GEOD.inv(
        node.longitude,
        node.latitude,
        goal.longitude,
        goal.latitude,
    )
    return geodesic_m / maximum_speed_mps


def _search(
    network: RoadNetwork,
    source: int,
    target: int,
    metric: PathMetric,
    *,
    use_heuristic: bool,
) -> RoadPath | None:
    if source not in network.nodes or target not in network.nodes:
        raise KeyError("Origem ou destino não pertence ao grafo")
    if source == target:
        return RoadPath(metric, 0.0, (source,), ())

    start_state = (source, 0)
    distances = {start_state: 0.0}
    previous: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
    initial_h = _heuristic(network, source, target, metric) if use_heuristic else 0.0
    queue: list[tuple[float, float, int, int]] = [(initial_h, 0.0, source, 0)]
    final_state: tuple[int, int] | None = None

    while queue:
        _, cost_so_far, node_id, incoming_way_id = heapq.heappop(queue)
        state = (node_id, incoming_way_id)
        if cost_so_far != distances.get(state):
            continue
        if node_id == target:
            final_state = state
            break

        for edge in network.adjacency[node_id]:
            if not network.allows_turn(incoming_way_id or None, node_id, edge.way_id):
                continue
            next_state = (edge.target, edge.way_id)
            next_cost = cost_so_far + _edge_cost(edge, metric)
            if next_cost >= distances.get(next_state, math.inf):
                continue
            distances[next_state] = next_cost
            previous[next_state] = (state, edge.edge_id)
            estimate = _heuristic(network, edge.target, target, metric) if use_heuristic else 0.0
            heapq.heappush(
                queue,
                (next_cost + estimate, next_cost, edge.target, edge.way_id),
            )

    if final_state is None:
        return None

    edge_ids: list[str] = []
    node_ids = [final_state[0]]
    state = final_state
    while state != start_state:
        prior_state, edge_id = previous[state]
        edge_ids.append(edge_id)
        node_ids.append(prior_state[0])
        state = prior_state
    edge_ids.reverse()
    node_ids.reverse()
    return RoadPath(metric, distances[final_state], tuple(node_ids), tuple(edge_ids))


def dijkstra(
    network: RoadNetwork,
    source: int,
    target: int,
    metric: PathMetric = "distance",
) -> RoadPath | None:
    return _search(network, source, target, metric, use_heuristic=False)


def astar(
    network: RoadNetwork,
    source: int,
    target: int,
    metric: PathMetric = "distance",
) -> RoadPath | None:
    return _search(network, source, target, metric, use_heuristic=True)


def all_reachable_targets(
    network: RoadNetwork,
    source: int,
    targets: set[int],
    metric: PathMetric = "distance",
) -> dict[int, RoadPath]:
    """Dijkstra from one source, stopping after every reachable target is settled."""
    if source not in network.nodes:
        raise KeyError(f"Origem ausente no grafo: {source}")
    missing = targets - network.nodes.keys()
    if missing:
        raise KeyError(f"Alvos ausentes no grafo: {sorted(missing)[:5]}")

    start_state = (source, 0)
    distances = {start_state: 0.0}
    previous: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
    queue: list[tuple[float, int, int]] = [(0.0, source, 0)]
    settled: dict[int, tuple[int, int]] = {}
    pending = set(targets)

    while queue and pending:
        cost_so_far, node_id, incoming_way_id = heapq.heappop(queue)
        state = (node_id, incoming_way_id)
        if cost_so_far != distances.get(state):
            continue
        if node_id in pending:
            settled[node_id] = state
            pending.remove(node_id)
        for edge in network.adjacency[node_id]:
            if not network.allows_turn(incoming_way_id or None, node_id, edge.way_id):
                continue
            next_state = (edge.target, edge.way_id)
            next_cost = cost_so_far + _edge_cost(edge, metric)
            if next_cost >= distances.get(next_state, math.inf):
                continue
            distances[next_state] = next_cost
            previous[next_state] = (state, edge.edge_id)
            heapq.heappush(queue, (next_cost, edge.target, edge.way_id))

    results: dict[int, RoadPath] = {}
    for target_id, final_state in settled.items():
        edge_ids: list[str] = []
        node_ids = [final_state[0]]
        state = final_state
        while state != start_state:
            prior_state, edge_id = previous[state]
            edge_ids.append(edge_id)
            node_ids.append(prior_state[0])
            state = prior_state
        edge_ids.reverse()
        node_ids.reverse()
        results[target_id] = RoadPath(
            metric,
            distances[final_state],
            tuple(node_ids),
            tuple(edge_ids),
        )
    return results
