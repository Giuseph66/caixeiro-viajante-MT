from __future__ import annotations

from app.optimization.visitation.tsp import solve_open_tsp, solve_open_tsp_exact
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
    return MetricMatrix(metric=metric, unit="m" if metric == "distance" else "s", costs=costs, paths=paths)


def test_open_tsp_uses_directed_costs_and_does_not_return_to_origin() -> None:
    seats = _seats(3)
    matrix = _matrix(
        [
            [0, 1, None],
            [None, 0, 1],
            [None, None, 0],
        ]
    )

    result = solve_open_tsp(
        seats,
        matrix,
        origin_ibge_code=5100000,
        objective="distance",
        time_limit_s=0.1,
        prove_small_instances=False,
    )

    assert result.status == "feasible"
    assert result.origin_ibge_code == 5100000
    assert result.visit_order == (5100000, 5100001, 5100002)
    assert result.objective_value == 2
    assert result.legs[-1].edge_ids == ("1>2",)
    assert result.total_time_s is None


def test_exact_tsp_proves_fixed_and_variable_origin_orders() -> None:
    seats = _seats(3)
    matrix = _matrix(
        [
            [0, 2, 8],
            [5, 0, 1],
            [2, 4, 0],
        ]
    )

    fixed = solve_open_tsp_exact(
        seats,
        matrix,
        origin_ibge_code=5100000,
        objective="distance",
    )
    variable = solve_open_tsp_exact(
        seats,
        matrix,
        origin_ibge_code=None,
        objective="distance",
    )

    assert fixed.status == "optimal_proven"
    assert fixed.candidates_evaluated == 2
    assert fixed.visit_order == (5100000, 5100001, 5100002)
    assert variable.status == "optimal_proven"
    assert variable.candidates_evaluated == 6
    assert sorted(variable.visit_order) == [5100000, 5100001, 5100002]


def test_exact_open_tsp_honors_fixed_destination() -> None:
    seats = _seats(3)
    matrix = _matrix(
        [
            [0, 2, 8],
            [8, 0, 1],
            [1, 8, 0],
        ]
    )

    result = solve_open_tsp_exact(
        seats,
        matrix,
        origin_ibge_code=5100000,
        destination_ibge_code=5100002,
        objective="distance",
    )

    assert result.status == "optimal_proven"
    assert result.visit_order == (5100000, 5100001, 5100002)
    assert result.candidates_evaluated == 1


def test_time_objective_counts_service_once_and_zero_at_origin() -> None:
    seats = _seats(3)
    matrix = _matrix(
        [
            [0, 3, 1],
            [3, 0, 1],
            [1, 1, 0],
        ],
        metric="time",
    )

    result = solve_open_tsp_exact(
        seats,
        matrix,
        origin_ibge_code=5100000,
        objective="time",
        service_times_s={5100000: 99, 5100001: 10, 5100002: 20},
    )

    assert result.visit_order == (5100000, 5100002, 5100001)
    assert result.travel_cost == 2
    assert result.total_service_time_s == 30
    assert result.objective_value == 32
    assert [leg.service_time_s for leg in result.legs] == [20, 10]
    assert result.total_time_s == 32


def test_exact_tsp_proves_disconnected_fixed_origin_infeasible() -> None:
    seats = _seats(3)
    matrix = _matrix(
        [
            [0, None, None],
            [1, 0, 1],
            [1, 1, 0],
        ]
    )

    result = solve_open_tsp_exact(
        seats,
        matrix,
        origin_ibge_code=5100000,
        objective="distance",
    )

    assert result.status == "infeasible_proven"
    assert result.visit_order == ()
    assert result.candidates_evaluated == 2


def test_variable_origin_ortools_visits_each_city_once() -> None:
    seats = _seats(4)
    matrix = _matrix(
        [
            [0, 2, 8, 9],
            [8, 0, 2, 9],
            [8, 9, 0, 2],
            [2, 8, 9, 0],
        ]
    )

    result = solve_open_tsp(
        seats,
        matrix,
        origin_ibge_code=None,
        objective="distance",
        time_limit_s=0.1,
        prove_small_instances=False,
    )

    assert result.status == "feasible"
    assert len(result.visit_order) == len(seats)
    assert len(set(result.visit_order)) == len(seats)
    assert result.origin_ibge_code == result.visit_order[0]
    assert len(result.legs) == len(seats) - 1

