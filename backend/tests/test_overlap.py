from __future__ import annotations

from app.optimization.overlap import (
    PhysicalEdgeMetric,
    RouteCandidate,
    assess_policy,
    measure_overlap,
    solve_route_pool,
)


def _edge(edge_id: str, physical_id: str, length_m: float) -> PhysicalEdgeMetric:
    return PhysicalEdgeMetric(edge_id, physical_id, length_m)


def _route(
    candidate_id: str,
    agent_id: int,
    city: int,
    edges: tuple[str, ...],
    distance_m: float,
    completion_time_s: float = 10.0,
) -> RouteCandidate:
    return RouteCandidate(
        candidate_id=candidate_id,
        agent_id=agent_id,
        served_city_codes=(city,),
        edge_ids=edges,
        distance_m=distance_m,
        completion_time_s=completion_time_s,
    )


def test_overlap_counts_opposite_directions_once_per_agent() -> None:
    metrics = {
        "forward": _edge("forward", "segment-a", 12.0),
        "reverse": _edge("reverse", "segment-a", 12.0),
        "private": _edge("private", "segment-b", 7.0),
    }
    routes = (
        _route("a", 0, 100, ("forward", "reverse", "forward"), 36.0),
        _route("b", 1, 200, ("reverse", "private"), 19.0),
    )

    result = measure_overlap(routes, metrics)

    assert result.redundant_distance_m == 12.0
    assert result.physical_segment_count == 2
    assert result.shared_segment_count == 1
    assert result.shared_segments[0].physical_segment_id == "segment-a"
    assert result.shared_segments[0].agent_count == 2


def test_p1_and_p2_lambda_zero_share_baseline_and_time_units_convert() -> None:
    metrics = {
        "a": _edge("a", "shared", 100.0),
        "b": _edge("b", "shared", 100.0),
    }
    routes = (
        _route("a", 0, 100, ("a",), 140.0, 20.0),
        _route("b", 1, 200, ("b",), 160.0, 30.0),
    )
    p1 = assess_policy(routes, metrics, objective="distance", policy="P1")
    p2_zero = assess_policy(
        routes, metrics, objective="distance", policy="P2", penalty_lambda=0
    )
    p2_time = assess_policy(
        routes, metrics, objective="time", policy="P2", penalty_lambda=2
    )

    assert p1.objective_value == p2_zero.objective_value == 300.0
    assert p1.overlap.redundant_distance_m == 100.0
    assert p2_time.base_objective == 30.0
    assert p2_time.penalty_value == 12.0
    assert p2_time.objective_value == 42.0


def test_p2_selects_less_shared_routes_from_candidate_pool() -> None:
    metrics = {
        "a-shared": _edge("a-shared", "shared", 100.0),
        "b-shared": _edge("b-shared", "shared", 100.0),
        "a-private": _edge("a-private", "private-a", 1.0),
        "b-private": _edge("b-private", "private-b", 1.0),
    }
    candidates = (
        _route("a-short", 0, 100, ("a-shared",), 5.0),
        _route("a-detour", 0, 100, ("a-private",), 30.0),
        _route("b-short", 1, 200, ("b-shared",), 5.0),
        _route("b-detour", 1, 200, ("b-private",), 30.0),
    )
    kwargs = {
        "required_city_codes": (100, 200),
        "agent_ids": (0, 1),
        "edge_metrics": metrics,
        "objective": "distance",
        "candidate_set_complete": True,
    }

    p1 = solve_route_pool(candidates, policy="P1", **kwargs)
    p2_zero = solve_route_pool(
        candidates, policy="P2", penalty_lambda=0, **kwargs
    )
    p2 = solve_route_pool(candidates, policy="P2", penalty_lambda=1, **kwargs)

    assert p1.status == p2_zero.status == p2.status == "optimal_proven"
    assert [route.candidate_id for route in p1.routes] == ["a-short", "b-short"]
    assert [route.candidate_id for route in p2_zero.routes] == [
        route.candidate_id for route in p1.routes
    ]
    assert [route.candidate_id for route in p2.routes] == [
        "a-detour",
        "b-short",
    ]
    assert p2.assessment is not None
    assert p2.assessment.overlap.redundant_distance_m == 0


def test_p3_uses_disjoint_candidates_and_checks_physical_ids() -> None:
    metrics = {
        "a-shared": _edge("a-shared", "shared", 10.0),
        "b-reverse": _edge("b-reverse", "shared", 10.0),
        "a-private": _edge("a-private", "private-a", 10.0),
        "b-private": _edge("b-private", "private-b", 10.0),
    }
    candidates = (
        _route("a-shared", 0, 100, ("a-shared",), 1.0),
        _route("a-clear", 0, 100, ("a-private",), 2.0),
        _route("b-reverse", 1, 200, ("b-reverse",), 1.0),
        _route("b-clear", 1, 200, ("b-private",), 2.0),
    )

    solution = solve_route_pool(
        candidates,
        required_city_codes=(100, 200),
        agent_ids=(0, 1),
        edge_metrics=metrics,
        objective="distance",
        policy="P3",
        candidate_set_complete=True,
    )

    assert solution.status == "optimal_proven"
    assert [route.candidate_id for route in solution.routes] == [
        "a-clear",
        "b-reverse",
    ]
    assert solution.assessment is not None
    assert solution.assessment.feasible
    assert solution.assessment.overlap.shared_segment_count == 0


def test_p3_reports_proof_only_for_complete_route_candidate_pool() -> None:
    metrics = {
        "a": _edge("a", "shared", 10.0),
        "b": _edge("b", "shared", 10.0),
    }
    candidates = (
        _route("a", 0, 100, ("a",), 1.0),
        _route("b", 1, 200, ("b",), 1.0),
    )
    args = {
        "required_city_codes": (100, 200),
        "agent_ids": (0, 1),
        "edge_metrics": metrics,
        "objective": "distance",
        "policy": "P3",
    }

    complete = solve_route_pool(candidates, candidate_set_complete=True, **args)
    partial = solve_route_pool(candidates, candidate_set_complete=False, **args)

    assert complete.status == "infeasible_proven"
    assert complete.proof_scope == "complete candidate pool"
    assert partial.status == "not_found"
    assert partial.proof_scope == "partial candidate pool"
