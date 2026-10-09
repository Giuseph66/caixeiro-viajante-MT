from __future__ import annotations

import gzip
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RoadNode:
    osm_id: int
    longitude: float
    latitude: float


@dataclass(frozen=True, slots=True)
class RoadEdge:
    edge_id: str
    source: int
    target: int
    way_id: int
    physical_segment_id: str
    length_m: float
    travel_time_s: float
    highway: str
    speed_kph: float
    speed_source: str
    tags: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class TurnRestriction:
    from_way_id: int
    via_node_id: int
    to_way_id: int
    kind: str


@dataclass(slots=True)
class RoadNetwork:
    nodes: dict[int, RoadNode]
    edges: dict[str, RoadEdge]
    turn_restrictions: tuple[TurnRestriction, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    adjacency: dict[int, tuple[RoadEdge, ...]] = field(init=False)
    _turn_rules: dict[tuple[int, int], tuple[frozenset[int], frozenset[int]]] = field(
        init=False, repr=False
    )
    max_speed_kph: float = field(init=False)

    def __post_init__(self) -> None:
        for node_id, node in self.nodes.items():
            if node.osm_id != node_id:
                raise ValueError(f"Chave do nó {node_id} diverge do identificador OSM")
            if (
                not math.isfinite(node.longitude)
                or not math.isfinite(node.latitude)
                or not -180 <= node.longitude <= 180
                or not -90 <= node.latitude <= 90
            ):
                raise ValueError(f"Nó OSM {node_id} possui coordenadas inválidas")

        outgoing: dict[int, list[RoadEdge]] = {node_id: [] for node_id in self.nodes}
        for edge_id, edge in self.edges.items():
            if not edge_id or edge.edge_id != edge_id or not edge.physical_segment_id:
                raise ValueError(f"Identificador inconsistente na aresta {edge_id!r}")
            if edge.source not in self.nodes or edge.target not in self.nodes:
                raise ValueError(f"Aresta {edge.edge_id} referencia nó ausente")
            if (
                not math.isfinite(edge.length_m)
                or not math.isfinite(edge.travel_time_s)
                or not math.isfinite(edge.speed_kph)
                or edge.length_m <= 0
                or edge.travel_time_s <= 0
                or edge.speed_kph <= 0
            ):
                raise ValueError(f"Aresta {edge.edge_id} possui custo inválido")
            expected_travel_time_s = edge.length_m / (edge.speed_kph / 3.6)
            if not math.isclose(
                edge.travel_time_s,
                expected_travel_time_s,
                rel_tol=1e-9,
                abs_tol=1e-6,
            ):
                raise ValueError(f"Aresta {edge.edge_id} possui distância, velocidade e tempo divergentes")
            outgoing[edge.source].append(edge)

        self.adjacency = {
            node_id: tuple(sorted(edges, key=lambda edge: edge.edge_id))
            for node_id, edges in outgoing.items()
        }
        rules: dict[tuple[int, int], tuple[set[int], set[int]]] = {}
        for restriction in self.turn_restrictions:
            if restriction.via_node_id not in self.nodes:
                raise ValueError(
                    f"Restrição de conversão referencia nó ausente: {restriction.via_node_id}"
                )
            key = (restriction.from_way_id, restriction.via_node_id)
            only, forbidden = rules.setdefault(key, (set(), set()))
            if restriction.kind == "only":
                only.add(restriction.to_way_id)
            elif restriction.kind == "no":
                forbidden.add(restriction.to_way_id)
            else:
                raise ValueError(f"Tipo de restrição desconhecido: {restriction.kind}")
        self._turn_rules = {
            key: (frozenset(only), frozenset(forbidden))
            for key, (only, forbidden) in rules.items()
        }
        self.max_speed_kph = max((edge.speed_kph for edge in self.edges.values()), default=0.0)

    def allows_turn(self, incoming_way_id: int | None, via_node_id: int, outgoing_way_id: int) -> bool:
        if incoming_way_id is None:
            return True
        only, forbidden = self._turn_rules.get((incoming_way_id, via_node_id), (frozenset(), frozenset()))
        if forbidden and outgoing_way_id in forbidden:
            return False
        return not only or outgoing_way_id in only

    def to_jsonl_gz(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
                header = {
                    "type": "header",
                    "format_version": 1,
                    "metadata": self.metadata,
                    "node_count": len(self.nodes),
                    "directed_edge_count": len(self.edges),
                    "turn_restriction_count": len(self.turn_restrictions),
                }
                compressed.write(
                    (json.dumps(header, sort_keys=True, separators=(",", ":")) + "\n").encode()
                )
                for node_id in sorted(self.nodes):
                    node = self.nodes[node_id]
                    record = {"type": "node", **asdict(node)}
                    compressed.write((json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode())
                for edge_id in sorted(self.edges):
                    record = {"type": "edge", **asdict(self.edges[edge_id])}
                    compressed.write((json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode())
                for restriction in sorted(
                    self.turn_restrictions,
                    key=lambda item: (item.from_way_id, item.via_node_id, item.to_way_id, item.kind),
                ):
                    record = {"type": "turn_restriction", **asdict(restriction)}
                    compressed.write((json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode())
        temporary.replace(path)

    @classmethod
    def from_jsonl_gz(cls, path: Path) -> RoadNetwork:
        nodes: dict[int, RoadNode] = {}
        edges: dict[str, RoadEdge] = {}
        restrictions: list[TurnRestriction] = []
        metadata: dict[str, Any] = {}
        expected_counts: dict[str, int] = {}
        with gzip.open(path, "rt", encoding="utf-8") as source:
            for index, line in enumerate(source):
                record = json.loads(line)
                record_type = record.pop("type", None)
                if index == 0:
                    if record_type != "header" or record.get("format_version") != 1:
                        raise ValueError("Cabeçalho ou versão do grafo inválidos")
                    metadata = record.get("metadata", {})
                    expected_counts = {
                        "node_count": int(record["node_count"]),
                        "directed_edge_count": int(record["directed_edge_count"]),
                        "turn_restriction_count": int(record["turn_restriction_count"]),
                    }
                    continue
                if record_type == "node":
                    node = RoadNode(**record)
                    nodes[node.osm_id] = node
                elif record_type == "edge":
                    record["tags"] = tuple(tuple(item) for item in record.get("tags", ()))
                    edge = RoadEdge(**record)
                    edges[edge.edge_id] = edge
                elif record_type == "turn_restriction":
                    restrictions.append(TurnRestriction(**record))
                else:
                    raise ValueError(f"Registro desconhecido no grafo: {record_type!r}")
        graph = cls(nodes, edges, tuple(restrictions), metadata)
        actual_counts = {
            "node_count": len(graph.nodes),
            "directed_edge_count": len(graph.edges),
            "turn_restriction_count": len(graph.turn_restrictions),
        }
        if actual_counts != expected_counts:
            raise ValueError("Quantidades do grafo divergem do cabeçalho")
        return graph
