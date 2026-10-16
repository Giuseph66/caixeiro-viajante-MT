from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from app.optimization.repetition import RepetitionPolicy, repeated_distance, repetition_penalty

OverlapPolicy = Literal["P1", "P2", "P3"]
RoutingObjective = Literal["distance", "time"]
OVERLAP_ALGORITHM_VERSION = "route-pool-overlap-v1"


@dataclass(frozen=True, slots=True)
class PhysicalEdgeMetric:
    edge_id: str
    physical_segment_id: str
    length_m: float
    source_node: int | None = None
    target_node: int | None = None
    travel_time_s: float | None = None


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    candidate_id: str
    agent_id: int
    served_city_codes: tuple[int, ...]
    edge_ids: tuple[str, ...]
    distance_m: float
    completion_time_s: float


@dataclass(frozen=True, slots=True)
class SharedSegment:
    physical_segment_id: str
    agent_count: int
    length_m: float


@dataclass(frozen=True, slots=True)
class OverlapMetrics:
    redundant_distance_m: float
    physical_segment_count: int
    shared_segments: tuple[SharedSegment, ...]

    @property
    def shared_segment_count(self) -> int:
        return len(self.shared_segments)


@dataclass(frozen=True, slots=True)
class PolicyAssessment:
    policy: OverlapPolicy
    objective: RoutingObjective
    base_objective: float
    overlap: OverlapMetrics
    penalty_value: float
    objective_value: float
    feasible: bool
    repeated_distance_m: float = 0.0
    repetition_penalty_value: float = 0.0


@dataclass(frozen=True, slots=True)
class RoutePoolSolution:
    status: Literal["feasible", "not_found", "optimal_proven", "infeasible_proven"]
    proof_scope: str
    routes: tuple[RouteCandidate, ...]
    assessment: PolicyAssessment | None
    complete_combinations_examined: int


def measure_overlap(
    routes: Sequence[RouteCandidate],
    edge_metrics: Mapping[str, PhysicalEdgeMetric],
) -> OverlapMetrics:
    """Measure shared physical road length once per additional agent.

    A route may traverse one physical segment repeatedly; it counts once for
    that agent. Reverse directed edges count as the same segment when their
    physical_segment_id matches.
    """
    if not routes:
        raise ValueError("É necessário ao menos uma rota")

    agent_ids = [route.agent_id for route in routes]
    if len(agent_ids) != len(set(agent_ids)):
        raise ValueError("Cada agente deve aparecer em exatamente uma rota")

    segment_lengths: dict[str, float] = {}
    segment_agents: dict[str, set[int]] = {}
    for route in routes:
        for edge_id in route.edge_ids:
            metric = edge_metrics.get(edge_id)
            if metric is None:
                raise ValueError(f"Métrica física ausente para a aresta {edge_id!r}")
            if metric.edge_id != edge_id or not metric.physical_segment_id:
                raise ValueError(f"Identidade física inválida para a aresta {edge_id!r}")
            if not math.isfinite(metric.length_m) or metric.length_m <= 0:
                raise ValueError(f"Comprimento inválido para a aresta {edge_id!r}")
            prior_length = segment_lengths.setdefault(
                metric.physical_segment_id, metric.length_m
            )
            if not math.isclose(prior_length, metric.length_m, rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError(
                    "Comprimentos direcionais divergem no segmento físico "
                    f"{metric.physical_segment_id!r}"
                )
            segment_agents.setdefault(metric.physical_segment_id, set()).add(
                route.agent_id
            )

    shared = tuple(
        SharedSegment(segment_id, len(agents), segment_lengths[segment_id])
        for segment_id, agents in sorted(segment_agents.items())
        if len(agents) > 1
    )
    redundant_distance_m = sum(
        (segment.agent_count - 1) * segment.length_m for segment in shared
    )
    return OverlapMetrics(
        redundant_distance_m=redundant_distance_m,
        physical_segment_count=len(segment_agents),
        shared_segments=shared,
    )


def assess_policy(
    routes: Sequence[RouteCandidate],
    edge_metrics: Mapping[str, PhysicalEdgeMetric],
    *,
    objective: RoutingObjective,
    policy: OverlapPolicy,
    penalty_lambda: float = 1.0,
    reference_speed_kph: float = 60.0,
    repetition_policy: RepetitionPolicy = "allow",
) -> PolicyAssessment:
    _validate_policy_inputs(objective, policy, penalty_lambda, reference_speed_kph)
    if not routes:
        raise ValueError("É necessário ao menos uma rota")
    for route in routes:
        _validate_route(route)

    overlap = measure_overlap(routes, edge_metrics)
    self_repeated = sum(repeated_distance(route.edge_ids, edge_metrics) for route in routes)
    self_penalty = repetition_penalty(self_repeated, objective, repetition_policy)
    distance_total_m = sum(route.distance_m for route in routes)
    time_max_s = max(route.completion_time_s for route in routes)
    base_objective = distance_total_m if objective == "distance" else time_max_s
    if policy == "P2":
        penalty_value = (
            penalty_lambda * overlap.redundant_distance_m
            if objective == "distance"
            else penalty_lambda
            * overlap.redundant_distance_m
            / (reference_speed_kph / 3.6)
        )
    else:
        penalty_value = 0.0
    return PolicyAssessment(
        policy=policy,
        objective=objective,
        base_objective=base_objective,
        overlap=overlap,
        penalty_value=penalty_value,
        objective_value=base_objective + penalty_value + self_penalty,
        feasible=(policy != "P3" or overlap.shared_segment_count == 0)
        and (repetition_policy != "forbid" or self_repeated <= 1e-6),
        repeated_distance_m=self_repeated,
        repetition_penalty_value=self_penalty,
    )


def solve_route_pool(
    route_candidates: Sequence[RouteCandidate],
    *,
    required_city_codes: Sequence[int],
    agent_ids: Sequence[int],
    edge_metrics: Mapping[str, PhysicalEdgeMetric],
    objective: RoutingObjective,
    policy: OverlapPolicy,
    penalty_lambda: float = 1.0,
    reference_speed_kph: float = 60.0,
    candidate_set_complete: bool = False,
    repetition_policy: RepetitionPolicy = "allow",
) -> RoutePoolSolution:
    """Exhaustively select active routes that cover each required city once.

    Proof statuses apply only to the supplied finite route-candidate pool.
    Global road-network proof requires a complete candidate set.
    """
    _validate_policy_inputs(objective, policy, penalty_lambda, reference_speed_kph)
    repetition_penalty(0, objective, repetition_policy)
    required = set(required_city_codes)
    agents = tuple(agent_ids)
    if not required or not agents or len(set(agents)) != len(agents):
        raise ValueError("Clientes e IDs de agentes devem ser não vazios e únicos")
    if len(required) != len(required_city_codes):
        raise ValueError("Cada município deve aparecer uma vez no conjunto de clientes")

    grouped: dict[int, list[RouteCandidate]] = {agent_id: [] for agent_id in agents}
    candidate_ids: set[str] = set()
    segment_lengths: dict[str, float] = {}
    candidate_segments: dict[str, frozenset[str]] = {}
    candidate_repetition: dict[str, float] = {}
    for candidate in route_candidates:
        if candidate.agent_id not in grouped:
            raise ValueError(f"Agente desconhecido na rota {candidate.candidate_id!r}")
        if candidate.candidate_id in candidate_ids:
            raise ValueError(f"ID de rota candidata duplicado: {candidate.candidate_id!r}")
        candidate_ids.add(candidate.candidate_id)
        _validate_route(candidate)
        if not candidate.served_city_codes:
            raise ValueError("Toda rota candidata deve atender ao menos um município")
        if len(set(candidate.served_city_codes)) != len(candidate.served_city_codes):
            raise ValueError("Uma rota candidata atende município duplicado")
        if not set(candidate.served_city_codes) <= required:
            raise ValueError("Rota candidata contém município fora do conjunto esperado")
        segments: set[str] = set()
        for edge_id in candidate.edge_ids:
            metric = edge_metrics.get(edge_id)
            if metric is None:
                raise ValueError(f"Métrica física ausente para a aresta {edge_id!r}")
            if (
                metric.edge_id != edge_id
                or not metric.physical_segment_id
                or not math.isfinite(metric.length_m)
                or metric.length_m <= 0
            ):
                raise ValueError(f"Métrica física inválida para a aresta {edge_id!r}")
            prior_length = segment_lengths.setdefault(
                metric.physical_segment_id, metric.length_m
            )
            if not math.isclose(prior_length, metric.length_m, rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError(
                    "Comprimentos direcionais divergem no segmento físico "
                    f"{metric.physical_segment_id!r}"
                )
            segments.add(metric.physical_segment_id)
        candidate_segments[candidate.candidate_id] = frozenset(segments)
        duplicate = repeated_distance(candidate.edge_ids, edge_metrics)
        candidate_repetition[candidate.candidate_id] = duplicate
        if repetition_policy == "forbid" and duplicate > 1e-6:
            continue
        grouped[candidate.agent_id].append(candidate)

    if any(not grouped[agent_id] for agent_id in agents):
        return _no_route_pool_solution(candidate_set_complete)

    for agent_id in agents:
        grouped[agent_id].sort(key=lambda item: item.candidate_id)

    best_routes: tuple[RouteCandidate, ...] | None = None
    best_objective_value: float | None = None
    selected: list[RouteCandidate] = []
    served: set[int] = set()
    used_segment_counts: dict[str, int] = {}
    minimum_future_distance = [
        min(candidate.distance_m for candidate in grouped[agent_id])
        for agent_id in agents
    ]
    complete_combinations_examined = 0

    def visit(
        agent_index: int,
        distance_so_far: float,
        time_max_so_far: float,
        overlap_so_far: float,
        repetition_so_far: float,
    ) -> None:
        nonlocal best_routes, best_objective_value, complete_combinations_examined
        if agent_index == len(agents):
            if served != required:
                return
            complete_combinations_examined += 1
            base_value = distance_so_far if objective == "distance" else time_max_so_far
            penalty_value = 0.0
            if policy == "P2":
                penalty_value = (
                    penalty_lambda * overlap_so_far
                    if objective == "distance"
                    else penalty_lambda
                    * overlap_so_far
                    / (reference_speed_kph / 3.6)
                )
            objective_value = base_value + penalty_value + repetition_penalty(repetition_so_far, objective, repetition_policy)
            tie_key = tuple(route.candidate_id for route in selected)
            best_tie_key = (
                tuple(route.candidate_id for route in best_routes)
                if best_routes is not None
                else None
            )
            if (
                best_objective_value is None
                or objective_value < best_objective_value - 1e-9
                or (
                    math.isclose(
                        objective_value,
                        best_objective_value,
                        rel_tol=1e-12,
                        abs_tol=1e-9,
                    )
                    and (best_tie_key is None or tie_key < best_tie_key)
                )
            ):
                best_routes = tuple(selected)
                best_objective_value = objective_value
            return

        lower_bound = (
            distance_so_far + sum(minimum_future_distance[agent_index:])
            if objective == "distance"
            else time_max_so_far
        )
        if policy == "P2":
            lower_bound += (
                penalty_lambda * overlap_so_far
                if objective == "distance"
                else penalty_lambda
                * overlap_so_far
                / (reference_speed_kph / 3.6)
            )
        lower_bound += repetition_penalty(repetition_so_far, objective, repetition_policy)
        if best_objective_value is not None and lower_bound > best_objective_value + 1e-9:
            return

        agent_id = agents[agent_index]
        for candidate in grouped[agent_id]:
            candidate_cities = set(candidate.served_city_codes)
            if served.intersection(candidate_cities):
                continue
            segments = candidate_segments[candidate.candidate_id]
            if policy == "P3" and any(
                used_segment_counts.get(segment_id, 0) for segment_id in segments
            ):
                continue

            selected.append(candidate)
            served.update(candidate_cities)
            additional_overlap = 0.0
            for segment_id in segments:
                if used_segment_counts.get(segment_id, 0):
                    additional_overlap += segment_lengths[segment_id]
                used_segment_counts[segment_id] = used_segment_counts.get(segment_id, 0) + 1
            visit(
                agent_index + 1,
                distance_so_far + candidate.distance_m,
                max(time_max_so_far, candidate.completion_time_s),
                overlap_so_far + additional_overlap,
                repetition_so_far + candidate_repetition[candidate.candidate_id],
            )
            for segment_id in segments:
                remaining_count = used_segment_counts[segment_id] - 1
                if remaining_count:
                    used_segment_counts[segment_id] = remaining_count
                else:
                    del used_segment_counts[segment_id]
            served.difference_update(candidate_cities)
            selected.pop()

    visit(0, 0.0, 0.0, 0.0, 0.0)
    if best_routes is None:
        status = "infeasible_proven" if candidate_set_complete else "not_found"
        return RoutePoolSolution(
            status=status,
            proof_scope="complete candidate pool" if candidate_set_complete else "partial candidate pool",
            routes=(),
            assessment=None,
            complete_combinations_examined=complete_combinations_examined,
        )
    best_assessment = assess_policy(
        best_routes,
        edge_metrics,
        objective=objective,
        policy=policy,
        penalty_lambda=penalty_lambda,
        reference_speed_kph=reference_speed_kph,
        repetition_policy=repetition_policy,
    )
    return RoutePoolSolution(
        status="optimal_proven" if candidate_set_complete else "feasible",
        proof_scope="complete candidate pool" if candidate_set_complete else "partial candidate pool",
        routes=best_routes,
        assessment=best_assessment,
        complete_combinations_examined=complete_combinations_examined,
    )


def _no_route_pool_solution(candidate_set_complete: bool) -> RoutePoolSolution:
    return RoutePoolSolution(
        status="infeasible_proven" if candidate_set_complete else "not_found",
        proof_scope="complete candidate pool" if candidate_set_complete else "partial candidate pool",
        routes=(),
        assessment=None,
        complete_combinations_examined=0,
    )


def _validate_policy_inputs(
    objective: RoutingObjective,
    policy: OverlapPolicy,
    penalty_lambda: float,
    reference_speed_kph: float,
) -> None:
    if objective not in ("distance", "time"):
        raise ValueError(f"Objetivo desconhecido: {objective!r}")
    if policy not in ("P1", "P2", "P3"):
        raise ValueError(f"Política desconhecida: {policy!r}")
    if not math.isfinite(penalty_lambda) or penalty_lambda < 0:
        raise ValueError("lambda deve ser finito e não negativo")
    if not math.isfinite(reference_speed_kph) or reference_speed_kph <= 0:
        raise ValueError("Velocidade de referência deve ser finita e positiva")


def _validate_route(route: RouteCandidate) -> None:
    if not route.candidate_id or route.agent_id < 0:
        raise ValueError("Identificador de rota/agente inválido")
    for label, value in (
        ("distância", route.distance_m),
        ("tempo de conclusão", route.completion_time_s),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{label.capitalize()} inválida na rota {route.candidate_id!r}")
