from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from time import perf_counter
from typing import Callable, Literal

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from app.routing.matrices import MetricMatrix, SeatSnap
from app.optimization.visitation.tsp import (
    TspLeg,
    TspObjective,
    TspStatus,
)

MtspMethod = Literal["ortools", "nearest_neighbor", "exact", "none"]
_COST_SCALE = 1_000_000
_MAX_SCALED_ARC = 1 << 50
_FORBIDDEN_ARC_COST = 1 << 60
_MAX_CUMULATIVE_COST = 1 << 61
MTSP_ALGORITHM_VERSION = "open-mtsp-ortools-v2-seeded"


@dataclass(frozen=True, slots=True)
class MtspRoute:
    agent_id: int
    visit_order: tuple[int, ...]
    legs: tuple[TspLeg, ...]
    travel_cost: float
    service_time_s: float
    objective_value: float


@dataclass(frozen=True, slots=True)
class MtspSolution:
    status: TspStatus
    objective: TspObjective
    unit: str
    origin_policy: Literal["fixed", "optimized"]
    origin_ibge_code: int | None
    vehicle_count: int
    routes: tuple[MtspRoute, ...]
    objective_value: float | None
    runtime_s: float
    method: MtspMethod
    candidate_origins_evaluated: int
    candidate_models_evaluated: int
    candidate_assignments_evaluated: int = 0
    seed: int = 0
    solver_statuses: tuple[int, ...] = ()
    heuristic_objective_value: float | None = None
    heuristic_origin_ibge_code: int | None = None
    heuristic_route_objective_values: tuple[float, ...] = ()
    ortools_objective_value: float | None = None
    ortools_origin_ibge_code: int | None = None
    ortools_route_objective_values: tuple[float, ...] = ()


def solve_open_mtsp(
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    *,
    vehicle_count: int,
    origin_ibge_code: int | None,
    objective: TspObjective,
    service_times_s: dict[int, float] | None = None,
    time_limit_s: float = 60.0,
    seed: int = 20261008,
    prove_small_instances: bool = True,
    exact_city_limit: int = 8,
    progress_callback: Callable[[int, int, int], None] | None = None,
    incumbent_callback: Callable[
        [tuple[tuple[int, ...], ...], float, int], None
    ] | None = None,
    include_edge_paths: bool = True,
) -> MtspSolution:
    """Solve an open mTSP with one common origin and every vehicle active."""
    started = perf_counter()
    services = _validate_inputs(
        seats, matrix, vehicle_count, origin_ibge_code, objective, service_times_s
    )
    _validate_search_limits(time_limit_s, seed, exact_city_limit)

    if len(seats) - 1 < vehicle_count:
        return _empty_solution(
            seats,
            matrix,
            vehicle_count,
            origin_ibge_code,
            objective,
            status="infeasible_proven",
            method="exact",
            seed=seed,
            runtime_s=perf_counter() - started,
        )

    if prove_small_instances and len(seats) <= exact_city_limit:
        return solve_open_mtsp_exact(
            seats,
            matrix,
            vehicle_count=vehicle_count,
            origin_ibge_code=origin_ibge_code,
            objective=objective,
            service_times_s=services,
            max_cities=exact_city_limit,
            seed=seed,
            include_edge_paths=include_edge_paths,
        )

    code_to_index = {seat.ibge_code: index for index, seat in enumerate(seats)}
    candidate_origins = (
        [code_to_index[origin_ibge_code]]
        if origin_ibge_code is not None
        else sorted(range(len(seats)), key=lambda index: seats[index].ibge_code)
    )
    per_model_limit_s = time_limit_s / len(candidate_origins)
    best_baseline: tuple[float, tuple[tuple[int, ...], ...], int] | None = None
    best_ortools: tuple[float, tuple[tuple[int, ...], ...], int] | None = None
    solver_statuses: list[int] = []
    best_reported_value = math.inf

    def report_incumbent(
        routes: tuple[tuple[int, ...], ...], origin_index: int
    ) -> None:
        nonlocal best_reported_value
        if incumbent_callback is None:
            return
        value = _objective_value(routes, seats, matrix, objective, services)
        if value is None or value >= best_reported_value:
            return
        best_reported_value = value
        incumbent_callback(
            tuple(
                tuple(seats[index].ibge_code for index in route)
                for route in routes
            ),
            value,
            seats[origin_index].ibge_code,
        )

    for candidate_number, origin_index in enumerate(candidate_origins, start=1):
        baseline_routes = _balanced_nearest_neighbor(
            seats, matrix, objective, services, origin_index, vehicle_count
        )
        if baseline_routes is not None:
            baseline_value = _objective_value(
                baseline_routes, seats, matrix, objective, services
            )
            if baseline_value is not None:
                baseline_candidate = (baseline_value, baseline_routes, origin_index)
                if _is_better(baseline_candidate, best_baseline, seats):
                    best_baseline = baseline_candidate
                report_incumbent(baseline_routes, origin_index)

        ortools_routes, solver_status = _solve_with_ortools(
            seats,
            matrix,
            objective,
            services,
            origin_index,
            vehicle_count,
            per_model_limit_s,
            seed,
            baseline_routes,
            lambda routes: report_incumbent(routes, origin_index),
        )
        solver_statuses.append(solver_status)
        if progress_callback is not None:
            progress_callback(
                candidate_number,
                len(candidate_origins),
                seats[origin_index].ibge_code,
            )
        if ortools_routes is not None:
            ortools_value = _objective_value(
                ortools_routes, seats, matrix, objective, services
            )
            if ortools_value is not None:
                ortools_candidate = (ortools_value, ortools_routes, origin_index)
                if _is_better(ortools_candidate, best_ortools, seats):
                    best_ortools = ortools_candidate

    candidates = [
        ("nearest_neighbor", best_baseline),
        ("ortools", best_ortools),
    ]
    available = [(method, item) for method, item in candidates if item is not None]
    if not available:
        return _empty_solution(
            seats,
            matrix,
            vehicle_count,
            origin_ibge_code,
            objective,
            status="not_found",
            method="none",
            seed=seed,
            runtime_s=perf_counter() - started,
            candidate_origins_evaluated=len(candidate_origins),
            candidate_models_evaluated=len(solver_statuses),
            solver_statuses=tuple(solver_statuses),
        )

    method, best_candidate = min(
        available,
        key=lambda item: (
            item[1][0],
            _route_codes(item[1][1], seats),
            0 if item[0] == "ortools" else 1,
        ),
    )
    best_value, best_routes, candidate_origin = best_candidate
    chosen_origin = seats[best_routes[0][0]].ibge_code
    if candidate_origin != best_routes[0][0]:
        raise ValueError("Origem da solução diverge do candidato avaliado")
    return _make_solution(
        best_routes,
        seats,
        matrix,
        objective,
        services,
        vehicle_count=vehicle_count,
        origin_policy="fixed" if origin_ibge_code is not None else "optimized",
        origin_ibge_code=chosen_origin,
        status="feasible",
        method=method,
        objective_value=best_value,
        runtime_s=perf_counter() - started,
        candidate_origins_evaluated=len(candidate_origins),
        candidate_models_evaluated=len(solver_statuses),
        seed=seed,
        solver_statuses=tuple(solver_statuses),
        heuristic_objective_value=(best_baseline[0] if best_baseline else None),
        heuristic_origin_ibge_code=(
            seats[best_baseline[2]].ibge_code if best_baseline else None
        ),
        heuristic_route_objective_values=(
            _route_objective_values(
                best_baseline[1], seats, matrix, objective, services
            )
            or ()
            if best_baseline
            else ()
        ),
        ortools_objective_value=(best_ortools[0] if best_ortools else None),
        ortools_origin_ibge_code=(
            seats[best_ortools[2]].ibge_code if best_ortools else None
        ),
        ortools_route_objective_values=(
            _route_objective_values(
                best_ortools[1], seats, matrix, objective, services
            )
            or ()
            if best_ortools
            else ()
        ),
        include_edge_paths=include_edge_paths,
    )


def solve_open_mtsp_exact(
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    *,
    vehicle_count: int,
    origin_ibge_code: int | None,
    objective: TspObjective,
    service_times_s: dict[int, float] | None = None,
    max_cities: int = 8,
    seed: int = 20261008,
    include_edge_paths: bool = True,
) -> MtspSolution:
    """Enumerate ordered, nonempty route partitions for small instances."""
    started = perf_counter()
    services = _validate_inputs(
        seats, matrix, vehicle_count, origin_ibge_code, objective, service_times_s
    )
    if max_cities < 1 or len(seats) > max_cities:
        raise ValueError(f"Enumeração limitada a {max_cities} cidades")
    _validate_seed(seed)

    if len(seats) - 1 < vehicle_count:
        return _empty_solution(
            seats,
            matrix,
            vehicle_count,
            origin_ibge_code,
            objective,
            status="infeasible_proven",
            method="exact",
            seed=seed,
            runtime_s=perf_counter() - started,
        )

    code_to_index = {seat.ibge_code: index for index, seat in enumerate(seats)}
    candidate_origins = (
        [code_to_index[origin_ibge_code]]
        if origin_ibge_code is not None
        else sorted(range(len(seats)), key=lambda index: seats[index].ibge_code)
    )
    best_value = math.inf
    best_routes: tuple[tuple[int, ...], ...] | None = None
    evaluated = 0

    for origin_index in candidate_origins:
        customers = sorted(
            (index for index in range(len(seats)) if index != origin_index),
            key=lambda index: seats[index].ibge_code,
        )
        for order in itertools.permutations(customers):
            for cuts in itertools.combinations(
                range(1, len(order)), vehicle_count - 1
            ):
                evaluated += 1
                boundaries = (*cuts, len(order))
                routes: list[tuple[int, ...]] = []
                start = 0
                for end in boundaries:
                    routes.append((origin_index, *order[start:end]))
                    start = end
                candidate_routes = tuple(routes)
                value = _objective_value(
                    candidate_routes, seats, matrix, objective, services
                )
                if value is None:
                    continue
                if value < best_value or (
                    value == best_value
                    and best_routes is not None
                    and _route_codes(candidate_routes, seats)
                    < _route_codes(best_routes, seats)
                ):
                    best_value = value
                    best_routes = candidate_routes

    if best_routes is None:
        return _empty_solution(
            seats,
            matrix,
            vehicle_count,
            origin_ibge_code,
            objective,
            status="infeasible_proven",
            method="exact",
            seed=seed,
            runtime_s=perf_counter() - started,
            candidate_origins_evaluated=len(candidate_origins),
            candidate_assignments_evaluated=evaluated,
        )

    return _make_solution(
        best_routes,
        seats,
        matrix,
        objective,
        services,
        vehicle_count=vehicle_count,
        origin_policy="fixed" if origin_ibge_code is not None else "optimized",
        origin_ibge_code=seats[best_routes[0][0]].ibge_code,
        status="optimal_proven",
        method="exact",
        objective_value=best_value,
        runtime_s=perf_counter() - started,
        candidate_origins_evaluated=len(candidate_origins),
        candidate_models_evaluated=0,
        candidate_assignments_evaluated=evaluated,
        seed=seed,
        include_edge_paths=include_edge_paths,
    )


def _validate_inputs(
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    vehicle_count: int,
    origin_ibge_code: int | None,
    objective: TspObjective,
    service_times_s: dict[int, float] | None,
) -> dict[int, float]:
    if type(vehicle_count) is not int or vehicle_count not in (2, 3):
        raise ValueError("A quantidade de agentes deve ser 2 ou 3")
    if objective not in ("distance", "time") or matrix.metric != objective:
        raise ValueError("A métrica da matriz deve coincidir com o objetivo")
    if not seats:
        raise ValueError("A instância precisa conter ao menos uma sede")
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
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("Tempos de serviço devem ser finitos e não negativos")
        services[code] = float(value)
    return services


def _validate_search_limits(
    time_limit_s: float, seed: int, exact_city_limit: int
) -> None:
    if not math.isfinite(time_limit_s) or time_limit_s <= 0:
        raise ValueError("O limite de tempo deve ser positivo e finito")
    _validate_seed(seed)
    if type(exact_city_limit) is not int or exact_city_limit < 1:
        raise ValueError("O limite de enumeração deve ser positivo")


def _validate_seed(seed: int) -> None:
    if type(seed) is not int or not 0 <= seed <= 2**31 - 1:
        raise ValueError("A semente deve ser um inteiro entre 0 e 2^31-1")


def _scaled_cost(value: float) -> int:
    scaled = round(value * _COST_SCALE)
    if scaled >= _MAX_SCALED_ARC:
        raise ValueError("Custo excede o limite inteiro aceito pelo OR-Tools")
    return scaled


def _solve_with_ortools(
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    objective: TspObjective,
    services: dict[int, float],
    origin_index: int,
    vehicle_count: int,
    time_limit_s: float,
    seed: int,
    initial_routes: tuple[tuple[int, ...], ...] | None,
    progress_callback: Callable[[tuple[tuple[int, ...], ...]], None] | None = None,
) -> tuple[tuple[tuple[int, ...], ...] | None, int]:
    size = len(seats)
    terminal_node = size
    node_count = size + 1
    manager = pywrapcp.RoutingIndexManager(
        node_count,
        vehicle_count,
        [origin_index] * vehicle_count,
        [terminal_node] * vehicle_count,
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
            if objective == "time":
                travel += services[seats[to_index].ibge_code]
            transit[from_index][to_index] = _scaled_cost(travel)

    transit_index = routing.RegisterTransitMatrix(transit)
    if objective == "distance":
        routing.SetArcCostEvaluatorOfAllVehicles(transit_index)
    else:
        zero_index = routing.RegisterTransitCallback(lambda _from, _to: 0)
        routing.SetArcCostEvaluatorOfAllVehicles(zero_index)
        routing.AddDimension(
            transit_index,
            0,
            _MAX_CUMULATIVE_COST,
            True,
            "CompletionTime",
        )
        routing.GetDimensionOrDie("CompletionTime").SetGlobalSpanCostCoefficient(1)

    for vehicle in range(vehicle_count):
        routing.solver().Add(routing.ActiveVehicleVar(vehicle) == 1)
    for from_city in range(size):
        from_indices = (
            [routing.Start(vehicle) for vehicle in range(vehicle_count)]
            if from_city == origin_index
            else [manager.NodeToIndex(from_city)]
        )
        for from_routing_index in from_indices:
            if from_routing_index < 0:
                continue
            for to_city in range(size):
                if from_city == to_city or matrix.costs[from_city][to_city] is not None:
                    continue
                to_routing_index = manager.NodeToIndex(to_city)
                if to_routing_index >= 0:
                    routing.NextVar(from_routing_index).RemoveValue(to_routing_index)

    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    whole_seconds = int(time_limit_s)
    parameters.time_limit.seconds = whole_seconds
    parameters.time_limit.nanos = int(
        (time_limit_s - whole_seconds) * 1_000_000_000
    )
    routing.solver().ReSeed(seed)
    assignment = None
    if progress_callback is not None:
        def on_solution() -> None:
            routes: list[tuple[int, ...]] = []
            for vehicle in range(vehicle_count):
                route: list[int] = []
                route_index = routing.Start(vehicle)
                while not routing.IsEnd(route_index):
                    node = manager.IndexToNode(route_index)
                    if node < size:
                        route.append(node)
                    route_index = routing.NextVar(route_index).Value()
                routes.append(tuple(route))
            progress_callback(tuple(routes))

        routing.AddAtSolutionCallback(on_solution)
    if initial_routes is not None:
        initial_assignment = routing.ReadAssignmentFromRoutes(
            [list(route[1:]) for route in initial_routes],
            True,
        )
        if initial_assignment is not None:
            assignment = routing.SolveFromAssignmentWithParameters(
                initial_assignment, parameters
            )
    if assignment is None:
        assignment = routing.SolveWithParameters(parameters)
    status = int(routing.status())
    if assignment is None:
        return None, status

    routes: list[tuple[int, ...]] = []
    for vehicle in range(vehicle_count):
        route: list[int] = []
        route_index = routing.Start(vehicle)
        while not routing.IsEnd(route_index):
            node = manager.IndexToNode(route_index)
            if node < size:
                route.append(node)
            route_index = assignment.Value(routing.NextVar(route_index))
        routes.append(tuple(route))
    result = tuple(routes)
    if not _valid_routes(result, size, origin_index, vehicle_count):
        return None, status
    return result, status


def _balanced_nearest_neighbor(
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    objective: TspObjective,
    services: dict[int, float],
    origin_index: int,
    vehicle_count: int,
) -> tuple[tuple[int, ...], ...] | None:
    routes: list[list[int]] = [[origin_index] for _ in range(vehicle_count)]
    loads = [0.0] * vehicle_count
    remaining = set(range(len(seats))) - {origin_index}

    for vehicle in range(vehicle_count):
        next_city = _nearest_reachable(
            seats, matrix, objective, services, routes[vehicle][-1], remaining
        )
        if next_city is None:
            return None
        _append_city(routes, loads, vehicle, next_city, matrix, objective, seats, services)
        remaining.remove(next_city)

    while remaining:
        reachable_vehicles = [
            vehicle
            for vehicle in range(vehicle_count)
            if any(matrix.costs[routes[vehicle][-1]][city] is not None for city in remaining)
        ]
        if not reachable_vehicles:
            return None
        vehicle = min(
            reachable_vehicles,
            key=lambda item: (loads[item], len(routes[item]), item),
        )
        next_city = _nearest_reachable(
            seats, matrix, objective, services, routes[vehicle][-1], remaining
        )
        if next_city is None:
            return None
        _append_city(routes, loads, vehicle, next_city, matrix, objective, seats, services)
        remaining.remove(next_city)
    result = tuple(tuple(route) for route in routes)
    return result if _valid_routes(result, len(seats), origin_index, vehicle_count) else None


def _nearest_reachable(
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    objective: TspObjective,
    services: dict[int, float],
    current: int,
    remaining: set[int],
) -> int | None:
    reachable = [city for city in remaining if matrix.costs[current][city] is not None]
    if not reachable:
        return None
    return min(
        reachable,
        key=lambda city: (
            matrix.costs[current][city]
            + (services[seats[city].ibge_code] if objective == "time" else 0.0),
            seats[city].ibge_code,
        ),
    )


def _append_city(
    routes: list[list[int]],
    loads: list[float],
    vehicle: int,
    city: int,
    matrix: MetricMatrix,
    objective: TspObjective,
    seats: list[SeatSnap],
    services: dict[int, float],
) -> None:
    travel = matrix.costs[routes[vehicle][-1]][city]
    assert travel is not None
    loads[vehicle] += travel
    if objective == "time":
        loads[vehicle] += services[seats[city].ibge_code]
    routes[vehicle].append(city)


def _objective_value(
    routes: tuple[tuple[int, ...], ...],
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    objective: TspObjective,
    services: dict[int, float],
) -> float | None:
    route_values = _route_objective_values(routes, seats, matrix, objective, services)
    if route_values is None or not route_values:
        return None
    return sum(route_values) if objective == "distance" else max(route_values)


def _route_objective_values(
    routes: tuple[tuple[int, ...], ...],
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    objective: TspObjective,
    services: dict[int, float],
) -> tuple[float, ...] | None:
    route_values: list[float] = []
    for route in routes:
        value = 0.0
        for from_index, to_index in zip(route, route[1:]):
            travel = matrix.costs[from_index][to_index]
            if travel is None:
                return None
            value += travel
            if objective == "time":
                value += services[seats[to_index].ibge_code]
        route_values.append(value)
    return tuple(route_values)


def _valid_routes(
    routes: tuple[tuple[int, ...], ...],
    city_count: int,
    origin_index: int,
    vehicle_count: int,
) -> bool:
    if len(routes) != vehicle_count or any(
        len(route) < 2 or route[0] != origin_index for route in routes
    ):
        return False
    visited = [city for route in routes for city in route[1:]]
    return len(visited) == city_count - 1 and set(visited) == (
        set(range(city_count)) - {origin_index}
    )


def _route_codes(
    routes: tuple[tuple[int, ...], ...], seats: list[SeatSnap]
) -> tuple[tuple[int, ...], ...]:
    return tuple(tuple(seats[index].ibge_code for index in route) for route in routes)


def _is_better(
    candidate: tuple[float, tuple[tuple[int, ...], ...], int],
    current: tuple[float, tuple[tuple[int, ...], ...], int] | None,
    seats: list[SeatSnap],
) -> bool:
    return current is None or (
        candidate[0], _route_codes(candidate[1], seats)
    ) < (current[0], _route_codes(current[1], seats))


def _make_solution(
    route_indices: tuple[tuple[int, ...], ...],
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    objective: TspObjective,
    services: dict[int, float],
    *,
    vehicle_count: int,
    origin_policy: Literal["fixed", "optimized"],
    origin_ibge_code: int,
    status: TspStatus,
    method: MtspMethod,
    objective_value: float,
    runtime_s: float,
    candidate_origins_evaluated: int,
    candidate_models_evaluated: int,
    candidate_assignments_evaluated: int = 0,
    seed: int = 0,
    solver_statuses: tuple[int, ...] = (),
    heuristic_objective_value: float | None = None,
    heuristic_origin_ibge_code: int | None = None,
    heuristic_route_objective_values: tuple[float, ...] = (),
    ortools_objective_value: float | None = None,
    ortools_origin_ibge_code: int | None = None,
    ortools_route_objective_values: tuple[float, ...] = (),
    include_edge_paths: bool = True,
) -> MtspSolution:
    result_routes: list[MtspRoute] = []
    for agent_id, route in enumerate(route_indices, start=1):
        legs: list[TspLeg] = []
        travel_total = 0.0
        service_total = 0.0
        for from_index, to_index in zip(route, route[1:]):
            travel = matrix.costs[from_index][to_index]
            path = matrix.paths[from_index][to_index]
            if travel is None or (include_edge_paths and path is None):
                raise ValueError("A rota contém um trecho dirigido inalcançável")
            service = services[seats[to_index].ibge_code]
            travel_total += travel
            service_total += service
            legs.append(
                TspLeg(
                    from_ibge_code=seats[from_index].ibge_code,
                    to_ibge_code=seats[to_index].ibge_code,
                    travel_cost=float(travel),
                    service_time_s=service,
                    edge_ids=tuple(path) if include_edge_paths and path is not None else (),
                )
            )
        route_objective = travel_total + (service_total if objective == "time" else 0.0)
        result_routes.append(
            MtspRoute(
                agent_id=agent_id,
                visit_order=tuple(seats[index].ibge_code for index in route),
                legs=tuple(legs),
                travel_cost=travel_total,
                service_time_s=service_total,
                objective_value=route_objective,
            )
        )
    return MtspSolution(
        status=status,
        objective=objective,
        unit=matrix.unit,
        origin_policy=origin_policy,
        origin_ibge_code=origin_ibge_code,
        vehicle_count=vehicle_count,
        routes=tuple(result_routes),
        objective_value=objective_value,
        runtime_s=runtime_s,
        method=method,
        candidate_origins_evaluated=candidate_origins_evaluated,
        candidate_models_evaluated=candidate_models_evaluated,
        candidate_assignments_evaluated=candidate_assignments_evaluated,
        seed=seed,
        solver_statuses=solver_statuses,
        heuristic_objective_value=heuristic_objective_value,
        heuristic_origin_ibge_code=heuristic_origin_ibge_code,
        heuristic_route_objective_values=heuristic_route_objective_values,
        ortools_objective_value=ortools_objective_value,
        ortools_origin_ibge_code=ortools_origin_ibge_code,
        ortools_route_objective_values=ortools_route_objective_values,
    )


def _empty_solution(
    seats: list[SeatSnap],
    matrix: MetricMatrix,
    vehicle_count: int,
    origin_ibge_code: int | None,
    objective: TspObjective,
    *,
    status: TspStatus,
    method: MtspMethod,
    seed: int,
    runtime_s: float,
    candidate_origins_evaluated: int = 0,
    candidate_models_evaluated: int = 0,
    candidate_assignments_evaluated: int = 0,
    solver_statuses: tuple[int, ...] = (),
) -> MtspSolution:
    return MtspSolution(
        status=status,
        objective=objective,
        unit=matrix.unit,
        origin_policy="fixed" if origin_ibge_code is not None else "optimized",
        origin_ibge_code=origin_ibge_code,
        vehicle_count=vehicle_count,
        routes=(),
        objective_value=None,
        runtime_s=runtime_s,
        method=method,
        candidate_origins_evaluated=candidate_origins_evaluated,
        candidate_models_evaluated=candidate_models_evaluated,
        candidate_assignments_evaluated=candidate_assignments_evaluated,
        seed=seed,
        solver_statuses=solver_statuses,
    )
