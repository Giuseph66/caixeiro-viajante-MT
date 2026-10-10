from __future__ import annotations

import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from app.optimization.visitation.mtsp import (
    solve_open_mtsp,
    solve_open_mtsp_exact,
)
from app.routing.matrices import MetricMatrix, SeatSnap


def _seats(count: int) -> list[SeatSnap]:
    return [
        SeatSnap(
            ibge_code=5100000 + index,
            municipality_name=f"Cidade {index}",
            seat_longitude=-56.0,
            seat_latitude=-12.0,
            node_id=index,
            snap_distance_m=0.0,
            within_10km=True,
        )
        for index in range(count)
    ]


def _matrix(
    costs: list[list[float | None]], *, metric: str = "distance"
) -> MetricMatrix:
    paths = [
        [
            None if cost is None else ([] if source == target else [f"{source}>{target}"])
            for target, cost in enumerate(row)
        ]
        for source, row in enumerate(costs)
    ]
    return MetricMatrix(
        metric=metric,
        unit="m" if metric == "distance" else "s",
        costs=costs,
        paths=paths,
    )


def _assert_valid_open_routes(result, city_count: int, origin_code: int) -> None:
    assert result.status in ("optimal_proven", "feasible")
    assert result.origin_ibge_code == origin_code
    assert len(result.routes) == result.vehicle_count
    assert all(len(route.visit_order) >= 2 for route in result.routes)
    assert all(route.visit_order[0] == origin_code for route in result.routes)
    served = [code for route in result.routes for code in route.visit_order[1:]]
    assert len(served) == city_count - 1
    assert len(set(served)) == len(served)
    assert set(served) == set(range(5100000, 5100000 + city_count)) - {origin_code}
    assert all(route.visit_order[-1] != origin_code for route in result.routes)


def test_exact_mtsp_has_open_routes_and_unique_service() -> None:
    seats = _seats(4)
    matrix = _matrix(
        [
            [0, 2, 3, None],
            [4, 0, 1, 2],
            [2, 5, 0, 1],
            [6, 6, 6, 0],
        ]
    )

    result = solve_open_mtsp_exact(
        seats,
        matrix,
        vehicle_count=2,
        origin_ibge_code=5100000,
        objective="distance",
    )

    _assert_valid_open_routes(result, len(seats), 5100000)
    assert result.status == "optimal_proven"
    assert result.objective_value == sum(route.travel_cost for route in result.routes)
    assert result.candidate_assignments_evaluated == 12
    assert all(len(route.legs) == len(route.visit_order) - 1 for route in result.routes)


def test_exact_mtsp_minimizes_max_time_and_counts_service_once() -> None:
    seats = _seats(4)
    matrix = _matrix(
        [
            [0, 1, 1, 1],
            [1, 0, 1, 1],
            [1, 1, 0, 1],
            [1, 1, 1, 0],
        ],
        metric="time",
    )

    result = solve_open_mtsp_exact(
        seats,
        matrix,
        vehicle_count=2,
        origin_ibge_code=5100000,
        objective="time",
        service_times_s={5100000: 500, 5100001: 30, 5100002: 0, 5100003: 10},
    )

    _assert_valid_open_routes(result, len(seats), 5100000)
    assert result.status == "optimal_proven"
    assert result.objective_value == max(route.objective_value for route in result.routes)
    assert result.objective_value == 31
    assert sum(route.travel_cost for route in result.routes) == 3
    assert sum(route.service_time_s for route in result.routes) == 40


def test_variable_origin_exactly_evaluates_all_origins_and_three_agents() -> None:
    seats = _seats(5)
    matrix = _matrix(
        [
            [0, 2, 8, 9, 6],
            [8, 0, 2, 9, 6],
            [8, 9, 0, 2, 6],
            [2, 8, 9, 0, 6],
            [6, 6, 6, 6, 0],
        ]
    )

    result = solve_open_mtsp(
        seats,
        matrix,
        vehicle_count=3,
        origin_ibge_code=None,
        objective="distance",
    )

    assert result.status == "optimal_proven"
    assert result.origin_policy == "optimized"
    assert result.candidate_origins_evaluated == len(seats)
    assert result.origin_ibge_code is not None
    _assert_valid_open_routes(result, len(seats), result.origin_ibge_code)


def test_ortools_mtsp_uses_all_agents_with_shared_start() -> None:
    seats = _seats(6)
    matrix = _matrix(
        [
            [0 if i == j else abs(i - j) + 1 for j in range(6)]
            for i in range(6)
        ]
    )

    result = solve_open_mtsp(
        seats,
        matrix,
        vehicle_count=3,
        origin_ibge_code=5100000,
        objective="distance",
        time_limit_s=0.2,
        seed=1234,
        prove_small_instances=False,
    )

    _assert_valid_open_routes(result, len(seats), 5100000)
    assert result.status == "feasible"
    assert result.seed == 1234
    assert result.ortools_objective_value is not None
    assert result.candidate_models_evaluated == 1
    assert result.objective_value <= result.heuristic_objective_value
    assert result.ortools_objective_value <= result.heuristic_objective_value


def test_ortools_time_objective_reports_the_global_maximum_load() -> None:
    seats = _seats(6)
    matrix = _matrix(
        [
            [0 if i == j else abs(i - j) + 1 for j in range(6)]
            for i in range(6)
        ],
        metric="time",
    )

    result = solve_open_mtsp(
        seats,
        matrix,
        vehicle_count=2,
        origin_ibge_code=5100000,
        objective="time",
        service_times_s={5100000: 900, 5100001: 30, 5100002: 0, 5100003: 10},
        time_limit_s=0.2,
        seed=7,
        prove_small_instances=False,
    )

    _assert_valid_open_routes(result, len(seats), 5100000)
    assert result.objective_value == max(route.objective_value for route in result.routes)
    assert result.ortools_objective_value is not None
    assert result.objective_value <= result.heuristic_objective_value
    assert result.ortools_objective_value <= result.heuristic_objective_value
    assert sum(route.service_time_s for route in result.routes) == 40


def test_mtsp_solver_runs_in_spawned_process_without_edge_paths() -> None:
    seats = _seats(6)
    matrix = _matrix(
        [
            [0 if i == j else abs(i - j) + 1 for j in range(6)]
            for i in range(6)
        ]
    )

    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
    ) as executor:
        result = executor.submit(
            solve_open_mtsp,
            seats,
            matrix,
            vehicle_count=2,
            origin_ibge_code=5100000,
            objective="distance",
            time_limit_s=0.2,
            seed=9,
            prove_small_instances=False,
            include_edge_paths=False,
        ).result()

    _assert_valid_open_routes(result, len(seats), 5100000)
    assert result.ortools_objective_value is not None
    assert all(not leg.edge_ids for route in result.routes for leg in route.legs)


def test_exact_mtsp_proves_insufficient_active_agents_infeasible() -> None:
    seats = _seats(3)
    matrix = _matrix([[0, 1, 1], [1, 0, 1], [1, 1, 0]])

    result = solve_open_mtsp_exact(
        seats,
        matrix,
        vehicle_count=3,
        origin_ibge_code=5100000,
        objective="distance",
    )

    assert result.status == "infeasible_proven"
    assert result.routes == ()
    assert result.candidate_assignments_evaluated == 0
