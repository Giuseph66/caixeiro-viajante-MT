from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from time import perf_counter
from typing import Callable, Literal

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from app.routing.matrices import MetricMatrix, SeatSnap

TspObjective = Literal["distance", "time"]
TspStatus = Literal["optimal_proven", "feasible", "not_found", "infeasible_proven"]
TspMethod = Literal["ortools", "nearest_neighbor", "exact", "none"]
_COST_SCALE = 1_000_000
_MAX_SCALED_ARC = 1 << 50
_FORBIDDEN_ARC_COST = 1 << 60
TSP_ALGORITHM_VERSION = "open-tsp-ortools-v2-endpoint"


@dataclass(frozen=True, slots=True)
class TspLeg:
    from_ibge_code: int
    to_ibge_code: int
    travel_cost: float
    service_time_s: float
    edge_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TspSolution:
    status: TspStatus
    objective: TspObjective
    unit: str
    origin_ibge_code: int | None
    visit_order: tuple[int, ...]
    legs: tuple[TspLeg, ...]
    objective_value: float | None
    travel_cost: float | None
    total_service_time_s: float
    total_time_s: float | None
    runtime_s: float
    method: TspMethod
    candidates_evaluated: int
    solver_status: int | None = None
    heuristic_objective_value: float | None = None
    ortools_objective_value: float | None = None


def solve_open_tsp(
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    *,
    origin_ibge_code: int | None,
    destination_ibge_code: int | None = None,
    objective: TspObjective,
    service_times_s: dict[int, float] | None = None,
    time_limit_s: float = 60.0,
    prove_small_instances: bool = True,
    exact_city_limit: int = 9,
    seed: int | None = None,
    progress_callback: Callable[[tuple[int, ...], float], None] | None = None,
) -> TspSolution:
    """Solve an open TSP with optional fixed first and last municipalities."""
    started = perf_counter()
    services = _validate_inputs(seats, matrix, objective, origin_ibge_code, service_times_s)
    _validate_destination(seats, origin_ibge_code, destination_ibge_code)
    if not math.isfinite(time_limit_s) or time_limit_s <= 0:
        raise ValueError("O limite de tempo deve ser positivo e finito")
    if exact_city_limit < 1:
        raise ValueError("O limite de enumeração deve ser positivo")
    if seed is not None and not 0 <= seed <= 2**31 - 1:
        raise ValueError("A semente deve estar entre 0 e 2^31-1")

    if prove_small_instances and len(seats) <= exact_city_limit:
        return solve_open_tsp_exact(
            seats,
            matrix,
            origin_ibge_code=origin_ibge_code,
            destination_ibge_code=destination_ibge_code,
            objective=objective,
            service_times_s=services,
            max_cities=exact_city_limit,
            progress_callback=progress_callback,
        )

    code_to_index = {seat.ibge_code: index for index, seat in enumerate(seats)}
    fixed_start = (
        code_to_index[origin_ibge_code] if origin_ibge_code is not None else None
    )
    fixed_end = (
        code_to_index[destination_ibge_code]
        if destination_ibge_code is not None
        else None
    )
    candidate_starts = (
        [fixed_start]
        if fixed_start is not None
        else sorted(
            (index for index in range(len(seats)) if index != fixed_end),
            key=lambda index: seats[index].ibge_code,
        )
    )

    best_reported_value = math.inf

    def report_incumbent(order: tuple[int, ...]) -> None:
        nonlocal best_reported_value
        if progress_callback is None:
            return
        value = _route_objective(order, seats, matrix, objective, services)
        if value is not None and value < best_reported_value:
            best_reported_value = value
            progress_callback(tuple(seats[index].ibge_code for index in order), value)

    baseline_orders = [
        order
        for start_index in candidate_starts
        if (
            order := _nearest_neighbor_order(
                seats, matrix, start_index, destination_index=fixed_end
            )
        )
        is not None
    ]
    baseline_order = min(
        baseline_orders,
        key=lambda order: (_route_objective(order, seats, matrix, objective, services), order),
        default=None,
    )
    if baseline_order is not None:
        report_incumbent(baseline_order)

    ortools_order, solver_status = _solve_with_ortools(
        seats,
        matrix,
        objective,
        services,
        fixed_start,
        fixed_end,
        time_limit_s,
        seed,
        report_incumbent,
    )
    candidates = [order for order in (baseline_order, ortools_order) if order is not None]
    heuristic_value = (
        _route_objective(baseline_order, seats, matrix, objective, services)
        if baseline_order is not None
        else None
    )
    ortools_value = (
        _route_objective(ortools_order, seats, matrix, objective, services)
        if ortools_order is not None
        else None
    )
    if not candidates:
        return TspSolution(
            status="not_found",
            objective=objective,
            unit=matrix.unit,
            origin_ibge_code=origin_ibge_code,
            visit_order=(),
            legs=(),
            objective_value=None,
            travel_cost=None,
            total_service_time_s=0.0,
            total_time_s=None,
            runtime_s=perf_counter() - started,
            method="none",
            candidates_evaluated=len(candidate_starts),
            solver_status=solver_status,
        )

    best_order = min(
        candidates,
        key=lambda order: (
            _route_objective(order, seats, matrix, objective, services),
            order,
            0 if order == ortools_order else 1,
        ),
    )
    method: TspMethod = "ortools" if best_order == ortools_order else "nearest_neighbor"
    result = _make_solution(
        best_order,
        seats,
        matrix,
        objective,
        services,
        status="feasible",
        method=method,
        runtime_s=perf_counter() - started,
        candidates_evaluated=len(candidate_starts),
        solver_status=solver_status,
        heuristic_objective_value=heuristic_value,
        ortools_objective_value=ortools_value,
    )
    return result


def solve_open_tsp_exact(
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    *,
    origin_ibge_code: int | None,
    destination_ibge_code: int | None = None,
    objective: TspObjective,
    service_times_s: dict[int, float] | None = None,
    max_cities: int = 9,
    progress_callback: Callable[[tuple[int, ...], float], None] | None = None,
) -> TspSolution:
    """Enumerate every open order and return an honest proof status."""
    started = perf_counter()
    services = _validate_inputs(seats, matrix, objective, origin_ibge_code, service_times_s)
    _validate_destination(seats, origin_ibge_code, destination_ibge_code)
    if max_cities < 1 or len(seats) > max_cities:
        raise ValueError(f"Enumeração limitada a {max_cities} cidades")

    ordered_indices = sorted(range(len(seats)), key=lambda index: seats[index].ibge_code)
    start_indices = (
        [next(index for index in ordered_indices if seats[index].ibge_code == origin_ibge_code)]
        if origin_ibge_code is not None
        else [index for index in ordered_indices if seats[index].ibge_code != destination_ibge_code]
    )
    end_index = (
        next(index for index in ordered_indices if seats[index].ibge_code == destination_ibge_code)
        if destination_ibge_code is not None
        else None
    )
    orders = (
        (start, *tail, *((end_index,) if end_index is not None else ()))
        for start in start_indices
        for tail in itertools.permutations(
            index for index in ordered_indices if index not in (start, end_index)
        )
    )

    best_order: tuple[int, ...] | None = None
    best_value = math.inf
    evaluated = 0
    for order in orders:
        evaluated += 1
        value = _route_objective(order, seats, matrix, objective, services)
        if value is None:
            continue
        if value < best_value:
            best_order = order
            best_value = value
            if progress_callback is not None:
                progress_callback(
                    tuple(seats[index].ibge_code for index in order), value
                )

    if best_order is None:
        return TspSolution(
            status="infeasible_proven",
            objective=objective,
            unit=matrix.unit,
            origin_ibge_code=origin_ibge_code,
            visit_order=(),
            legs=(),
            objective_value=None,
            travel_cost=None,
            total_service_time_s=0.0,
            total_time_s=None,
            runtime_s=perf_counter() - started,
            method="exact",
            candidates_evaluated=evaluated,
        )

    return _make_solution(
        best_order,
        seats,
        matrix,
        objective,
        services,
        status="optimal_proven",
        method="exact",
        runtime_s=perf_counter() - started,
        candidates_evaluated=evaluated,
    )


def _validate_inputs(
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    objective: TspObjective,
    origin_ibge_code: int | None,
    service_times_s: dict[int, float] | None,
) -> dict[int, float]:
    if objective not in ("distance", "time") or matrix.metric != objective:
        raise ValueError("A métrica da matriz deve coincidir com o objetivo")
    if not seats:
        raise ValueError("A rota precisa conter ao menos uma sede")
    codes = [seat.ibge_code for seat in seats]
    if len(set(codes)) != len(codes):
        raise ValueError("Códigos IBGE duplicados")
    if origin_ibge_code is not None and origin_ibge_code not in set(codes):
        raise ValueError("A origem fixa não pertence às sedes informadas")
    size = len(seats)
    if (
        len(matrix.costs) != size
        or len(matrix.paths) != size
        or any(len(row) != size for row in matrix.costs)
        or any(len(row) != size for row in matrix.paths)
    ):
        raise ValueError("Dimensões da matriz divergem da lista de sedes")
    for costs, paths in zip(matrix.costs, matrix.paths, strict=True):
        for cost, path in zip(costs, paths, strict=True):
            if cost is None:
                if path is not None:
                    raise ValueError("Par inalcançável deve ter caminho nulo")
            elif (
                isinstance(cost, bool)
                or not isinstance(cost, (int, float))
                or not math.isfinite(cost)
                or cost < 0
                or path is None
                or any(not isinstance(edge_id, str) for edge_id in path)
            ):
                raise ValueError("Custo ou caminho inválido na matriz")

    supplied = service_times_s or {}
    if set(supplied) - set(codes):
        raise ValueError("Tempo de serviço informado para sede ausente")
    services: dict[int, float] = {}
    for code in codes:
        value = supplied.get(code, 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Tempos de serviço devem ser números")
        if not math.isfinite(value) or value < 0:
            raise ValueError("Tempos de serviço devem ser finitos e não negativos")
        services[code] = float(value)
    return services


def _scaled_cost(value: float) -> int:
    scaled = round(value * _COST_SCALE)
    if scaled >= _MAX_SCALED_ARC:
        raise ValueError("Custo excede o limite inteiro aceito pelo OR-Tools")
    return scaled


def _solve_with_ortools(
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    objective: TspObjective,
    service_times_s: dict[int, float],
    fixed_start: int | None,
    fixed_end: int | None,
    time_limit_s: float,
    seed: int | None,
    progress_callback: Callable[[tuple[int, ...]], None] | None = None,
) -> tuple[tuple[int, ...] | None, int]:
    size = len(seats)
    source_node = size if fixed_start is None else fixed_start
    terminal_node = size + (1 if fixed_start is None else 0)
    node_count = size + (2 if fixed_start is None else 1)
    manager = pywrapcp.RoutingIndexManager(
        node_count, 1, [source_node], [terminal_node]
    )
    routing = pywrapcp.RoutingModel(manager)

    transit: list[list[int]] = [[0] * node_count for _ in range(node_count)]
    for from_index in range(size):
        for to_index in range(size):
            if from_index == to_index:
                continue
            travel = matrix.costs[from_index][to_index]
            if travel is None:
                transit[from_index][to_index] = _FORBIDDEN_ARC_COST
                continue
            cost = travel
            if objective == "time":
                cost += service_times_s[seats[to_index].ibge_code]
            transit[from_index][to_index] = _scaled_cost(cost)
    transit_index = routing.RegisterTransitMatrix(transit)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_index)

    for from_city in range(size):
        from_routing_index = (
            routing.Start(0)
            if fixed_start == from_city
            else manager.NodeToIndex(from_city)
        )
        if from_routing_index < 0:
            continue
        for to_city in range(size):
            if from_city == to_city or matrix.costs[from_city][to_city] is not None:
                continue
            to_routing_index = manager.NodeToIndex(to_city)
            if to_routing_index >= 0:
                routing.NextVar(from_routing_index).RemoveValue(to_routing_index)

    if fixed_end is not None:
        end_routing_index = routing.End(0)
        for city in range(size):
            from_routing_index = (
                routing.Start(0)
                if fixed_start == city
                else manager.NodeToIndex(city)
            )
            if city != fixed_end:
                routing.NextVar(from_routing_index).RemoveValue(end_routing_index)
            elif from_routing_index >= 0:
                for other_city in range(size):
                    if other_city != fixed_end:
                        other_index = manager.NodeToIndex(other_city)
                        if other_index >= 0:
                            routing.NextVar(from_routing_index).RemoveValue(other_index)
        if fixed_start is None:
            routing.NextVar(routing.Start(0)).RemoveValue(manager.NodeToIndex(fixed_end))

    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    whole_seconds = int(time_limit_s)
    parameters.time_limit.seconds = whole_seconds
    parameters.time_limit.nanos = int((time_limit_s - whole_seconds) * 1_000_000_000)
    if seed is not None:
        routing.solver().ReSeed(seed)

    if progress_callback is not None:
        def on_solution() -> None:
            order: list[int] = []
            index = routing.Start(0)
            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                if node < size:
                    order.append(node)
                index = routing.NextVar(index).Value()
            progress_callback(tuple(order))

        routing.AddAtSolutionCallback(on_solution)
    solution = routing.SolveWithParameters(parameters)
    status = int(routing.status())
    if solution is None:
        return None, status

    order: list[int] = []
    route_index = routing.Start(0)
    while not routing.IsEnd(route_index):
        node = manager.IndexToNode(route_index)
        if node < size:
            order.append(node)
        route_index = solution.Value(routing.NextVar(route_index))
    if len(order) != size or (fixed_start is not None and order[0] != fixed_start):
        return None, status
    return tuple(order), status


def _nearest_neighbor_order(
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    start: int,
    *,
    destination_index: int | None = None,
) -> tuple[int, ...] | None:
    order = [start]
    remaining = set(range(len(seats))) - {start, destination_index}
    while remaining:
        current = order[-1]
        reachable = [
            target
            for target in remaining
            if matrix.costs[current][target] is not None
        ]
        if not reachable:
            return None
        next_city = min(
            reachable,
            key=lambda target: (
                matrix.costs[current][target],
                seats[target].ibge_code,
            ),
        )
        order.append(next_city)
        remaining.remove(next_city)
    if destination_index is not None:
        if matrix.costs[order[-1]][destination_index] is None:
            return None
        order.append(destination_index)
    return tuple(order)


def _validate_destination(
    seats: list[SeatSnap],
    origin_ibge_code: int | None,
    destination_ibge_code: int | None,
) -> None:
    if destination_ibge_code is None:
        return
    known_codes = {seat.ibge_code for seat in seats}
    if destination_ibge_code not in known_codes:
        raise ValueError("Destino não está presente na matriz de municípios")
    if destination_ibge_code == origin_ibge_code:
        raise ValueError("Origem e destino precisam ser municípios diferentes")


def _route_objective(
    order: tuple[int, ...],
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    objective: TspObjective,
    service_times_s: dict[int, float],
) -> float | None:
    total = 0.0
    for from_index, to_index in zip(order, order[1:], strict=False):
        travel = matrix.costs[from_index][to_index]
        if travel is None:
            return None
        total += travel
        if objective == "time":
            total += service_times_s[seats[to_index].ibge_code]
    return total


def _make_solution(
    order: tuple[int, ...],
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    objective: TspObjective,
    service_times_s: dict[int, float],
    *,
    status: TspStatus,
    method: TspMethod,
    runtime_s: float,
    candidates_evaluated: int,
    solver_status: int | None = None,
    heuristic_objective_value: float | None = None,
    ortools_objective_value: float | None = None,
) -> TspSolution:
    legs: list[TspLeg] = []
    travel_total = 0.0
    service_total = 0.0
    for from_index, to_index in zip(order, order[1:], strict=False):
        travel = matrix.costs[from_index][to_index]
        path = matrix.paths[from_index][to_index]
        if travel is None or path is None:
            raise ValueError("A ordem da rota contém um trecho inalcançável")
        service_time = service_times_s[seats[to_index].ibge_code]
        travel_total += travel
        service_total += service_time
        legs.append(
            TspLeg(
                from_ibge_code=seats[from_index].ibge_code,
                to_ibge_code=seats[to_index].ibge_code,
                travel_cost=float(travel),
                service_time_s=service_time,
                edge_ids=tuple(path),
            )
        )
    objective_value = travel_total + (service_total if objective == "time" else 0.0)
    return TspSolution(
        status=status,
        objective=objective,
        unit=matrix.unit,
        origin_ibge_code=seats[order[0]].ibge_code,
        visit_order=tuple(seats[index].ibge_code for index in order),
        legs=tuple(legs),
        objective_value=objective_value,
        travel_cost=travel_total,
        total_service_time_s=service_total,
        total_time_s=(objective_value if objective == "time" else None),
        runtime_s=runtime_s,
        method=method,
        candidates_evaluated=candidates_evaluated,
        solver_status=solver_status,
        heuristic_objective_value=heuristic_objective_value,
        ortools_objective_value=ortools_objective_value,
    )
