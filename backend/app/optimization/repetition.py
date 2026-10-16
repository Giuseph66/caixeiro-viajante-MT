from __future__ import annotations

import itertools
import math
import random
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from time import perf_counter
from typing import Callable, Literal, Mapping, Protocol, Sequence

from app.routing.matrices import MetricMatrix

RepetitionPolicy = Literal["allow", "reduce", "forbid"]
REPETITION_ALGORITHM_VERSION = "physical-route-repetition-v1"
REFERENCE_SPEED_MPS = 60 / 3.6


class PhysicalMetric(Protocol):
    physical_segment_id: str
    length_m: float


def repeated_distance(edge_ids: Sequence[str], metrics: Mapping[str, PhysicalMetric]) -> float:
    """Count every traversal after the first, including the opposite direction.

    Call separately for each agent: sharing between agents is a different metric.
    """
    seen: set[str] = set()
    total = 0.0
    for edge_id in edge_ids:
        edge = metrics[edge_id]
        if not edge.physical_segment_id or not math.isfinite(edge.length_m) or edge.length_m <= 0:
            raise ValueError(f"Métrica física inválida: {edge_id}")
        if edge.physical_segment_id in seen:
            total += edge.length_m
        seen.add(edge.physical_segment_id)
    return total


def repetition_penalty(distance_m: float, objective: str, policy: RepetitionPolicy) -> float:
    if policy not in ("allow", "reduce", "forbid"):
        raise ValueError(f"Regra de repetição desconhecida: {policy}")
    if objective not in ("distance", "time"):
        raise ValueError(f"Objetivo desconhecido: {objective}")
    return (
        (distance_m if objective == "distance" else distance_m / REFERENCE_SPEED_MPS)
        if policy == "reduce"
        else 0.0
    )


@dataclass(frozen=True)
class RepetitionResult:
    orders: tuple[tuple[int, ...], ...]
    repeated_distance_by_agent_m: tuple[float, ...]
    base_objective: float
    penalty_value: float
    objective_value: float
    feasible: bool
    candidates_evaluated: int
    runtime_s: float


def refine_route_repetition(
    orders: Sequence[Sequence[int]],
    city_codes: Sequence[int],
    matrix: MetricMatrix,
    edge_metrics: Mapping[str, PhysicalMetric],
    *,
    policy: RepetitionPolicy,
    time_limit_s: float,
    fixed_origin: bool = True,
    destination_ibge_code: int | None = None,
    service_times_s: Mapping[int, float] | None = None,
    seed: int = 0,
    progress_callback: Callable[[tuple[tuple[int, ...], ...], float], None] | None = None,
) -> RepetitionResult:
    """Refine city orders using cached directed paths and per-agent edge counts.

    This is a bounded heuristic over matrix paths, not a road-network proof.
    Only changed legs are rescored; the leg cache is bounded to limit RAM.
    """
    started = perf_counter()
    repetition_penalty(0, matrix.metric, policy)
    if not math.isfinite(time_limit_s) or time_limit_s <= 0:
        raise ValueError("O tempo de busca de repetição deve ser positivo e finito")
    indices = {code: i for i, code in enumerate(city_codes)}
    services = service_times_s or {}
    initial = tuple(tuple(order) for order in orders)
    if not initial or any(len(order) < 2 for order in initial):
        raise ValueError("Cada agente precisa de uma rota ativa")
    origin = initial[0][0]
    visits = [code for order in initial for code in order[1:]]
    if (
        len(indices) != len(city_codes)
        or any(order[0] != origin for order in initial)
        or len(visits) != len(indices) - 1
        or set(visits) != set(indices) - {origin}
        or len(set(visits)) != len(visits)
    ):
        raise ValueError("As rotas precisam cobrir cada cidade uma vez e ter origem comum")
    if destination_ibge_code is not None and (
        len(initial) != 1 or initial[0][-1] != destination_ibge_code
    ):
        raise ValueError("Destino fixo divergente ou aplicado a múltiplos agentes")

    lengths: dict[str, float] = {}

    @lru_cache(maxsize=128)
    def leg_counts(a: int, b: int) -> Counter[str]:
        i, j = indices[a], indices[b]
        if matrix.costs[i][j] is None or matrix.paths[i][j] is None:
            raise ValueError("Perna sem caminho dirigido")
        counts: Counter[str] = Counter()
        for edge_id in matrix.paths[i][j]:
            metric = edge_metrics[edge_id]
            if (
                not metric.physical_segment_id
                or not math.isfinite(metric.length_m)
                or metric.length_m <= 0
            ):
                raise ValueError(f"Métrica física inválida: {edge_id}")
            prior = lengths.setdefault(metric.physical_segment_id, metric.length_m)
            if not math.isclose(prior, metric.length_m, rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError("Comprimentos direcionais divergem no mesmo trecho físico")
            counts[metric.physical_segment_id] += 1
        return counts

    def arcs(order: tuple[int, ...]) -> Counter[tuple[int, int]]:
        return Counter(zip(order, order[1:]))

    def base_value(candidate: tuple[tuple[int, ...], ...]) -> float:
        loads = []
        for order in candidate:
            costs = [matrix.costs[indices[a]][indices[b]] for a, b in zip(order, order[1:])]
            if any(cost is None for cost in costs):
                return math.inf
            loads.append(
                sum(costs)
                + (
                    sum(services.get(code, 0.0) for code in order[1:])
                    if matrix.metric == "time"
                    else 0.0
                )
            )
        return sum(loads) if matrix.metric == "distance" else max(loads)

    current = initial
    current_arcs = tuple(arcs(order) for order in current)
    counts_by_agent: list[Counter[str]] = []
    repeated = []
    for route_arcs in current_arcs:
        counts: Counter[str] = Counter()
        for pair in route_arcs:
            counts.update(leg_counts(*pair))
        counts_by_agent.append(counts)
        repeated.append(sum((count - 1) * lengths[segment] for segment, count in counts.items()))

    def score(base: float, repeats: Sequence[float]) -> tuple[float, float]:
        duplicate = max(0.0, sum(repeats))
        return (
            (duplicate, base)
            if policy == "forbid"
            else (base + repetition_penalty(duplicate, matrix.metric, policy), duplicate)
        )

    current_base = base_value(current)
    current_score = score(current_base, repeated)
    evaluated = 1
    deadline = started + time_limit_s

    def consider(candidate: tuple[tuple[int, ...], ...]) -> None:
        nonlocal current, current_arcs, current_base, current_score, repeated, evaluated
        evaluated += 1
        base = base_value(candidate)
        if not math.isfinite(base):
            return
        next_arcs = tuple(arcs(order) for order in candidate)
        deltas: list[Counter[str]] = []
        next_repeated = list(repeated)
        for route_id, (before, after) in enumerate(zip(current_arcs, next_arcs, strict=True)):
            delta: Counter[str] = Counter()
            for pair in before - after:
                delta.subtract(leg_counts(*pair))
            for pair in after - before:
                delta.update(leg_counts(*pair))
            counts = counts_by_agent[route_id]
            for segment, change in delta.items():
                count = counts.get(segment, 0)
                next_repeated[route_id] += (
                    max(0, count + change - 1) - max(0, count - 1)
                ) * lengths[segment]
            next_repeated[route_id] = max(0.0, next_repeated[route_id])
            deltas.append(delta)
        candidate_score = score(base, next_repeated)
        if candidate_score >= current_score:
            return
        for counts, delta in zip(counts_by_agent, deltas, strict=True):
            counts.update(delta)
            for segment in delta:
                if counts[segment] == 0:
                    del counts[segment]
        current, current_arcs = candidate, next_arcs
        current_base, current_score, repeated = base, candidate_score, next_repeated
        if progress_callback is not None:
            progress_callback(
                current, current_base + repetition_penalty(sum(repeated), matrix.metric, policy)
            )

    if policy != "allow" and len(city_codes) <= 8 and len(initial) == 1:
        prefix = initial[0][:1] if fixed_origin else ()
        suffix = initial[0][-1:] if destination_ibge_code is not None else ()
        movable = [code for code in initial[0] if code not in prefix + suffix]
        for permutation in itertools.permutations(movable):
            if perf_counter() >= deadline:
                break
            consider((prefix + permutation + suffix,))
    elif policy != "allow":
        rng = random.Random(seed)
        for _ in range(20_000):
            if perf_counter() >= deadline:
                break
            candidate = [list(order) for order in current]
            positions = [
                (r, i)
                for r, order in enumerate(current)
                for i in range(
                    0 if len(current) == 1 and not fixed_origin else 1,
                    len(order) - (1 if destination_ibge_code is not None else 0),
                )
            ]
            if len(positions) < 2:
                break
            (ra, ia), (rb, ib) = rng.sample(positions, 2)
            move = rng.randrange(4)
            if move == 0 and len(current) > 1 and not fixed_origin:
                new_origin = candidate[ra][ia]
                candidate[ra][ia] = current[0][0]
                for route in candidate:
                    route[0] = new_origin
            elif move == 1 and ra == rb:
                lo, hi = sorted((ia, ib))
                candidate[ra][lo : hi + 1] = reversed(candidate[ra][lo : hi + 1])
            elif move == 2 and (ra == rb or len(candidate[ra]) > 2):
                city = candidate[ra].pop(ia)
                candidate[rb].insert(min(ib, len(candidate[rb])), city)
            else:
                candidate[ra][ia], candidate[rb][ib] = candidate[rb][ib], candidate[ra][ia]
            consider(tuple(tuple(order) for order in candidate))

    penalty = repetition_penalty(sum(repeated), matrix.metric, policy)
    return RepetitionResult(
        orders=current,
        repeated_distance_by_agent_m=tuple(repeated),
        base_objective=current_base,
        penalty_value=penalty,
        objective_value=current_base + penalty,
        feasible=policy != "forbid" or not any(value > 1e-6 for value in repeated),
        candidates_evaluated=evaluated,
        runtime_s=perf_counter() - started,
    )
