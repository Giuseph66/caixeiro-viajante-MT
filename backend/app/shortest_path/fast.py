from __future__ import annotations

import ctypes
import gc
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
from array import array
from pathlib import Path

from app.graph.network import RoadNetwork
from app.shortest_path.search import PathMetric


class NativeSearchUnavailable(RuntimeError):
    pass


class _NativeResult(ctypes.Structure):
    _fields_ = [
        ("costs", ctypes.POINTER(ctypes.c_double)),
        ("path_offsets", ctypes.POINTER(ctypes.c_uint64)),
        ("edge_indexes", ctypes.POINTER(ctypes.c_uint32)),
        ("edge_count", ctypes.c_uint64),
    ]


_COMPILE_LOCK = threading.Lock()
_COMPILER_FLAGS = ("-O3", "-DNDEBUG", "-std=c++17", "-shared", "-fPIC")


def _buffer_pointer(values: array, ctype: type[ctypes._SimpleCData]) -> ctypes._Pointer:
    if not values:
        return ctypes.POINTER(ctype)()
    return ctypes.cast(ctypes.c_void_p(values.buffer_info()[0]), ctypes.POINTER(ctype))


def _load_native_library() -> ctypes.CDLL:
    compiler = shutil.which("g++")
    if compiler is None:
        raise NativeSearchUnavailable("g++ não está instalado")

    source = Path(__file__).with_name("fast_dijkstra.cpp")
    if not source.is_file():
        raise NativeSearchUnavailable(f"Fonte C++ ausente: {source}")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    library_path = Path(tempfile.gettempdir()) / f"mt_fast_dijkstra_{source_digest}.so"
    with _COMPILE_LOCK:
        if not library_path.exists():
            temporary_path = library_path.with_name(
                f"{library_path.name}.{os.getpid()}.tmp"
            )
            try:
                subprocess.run(
                    [
                        compiler,
                        *_COMPILER_FLAGS,
                        str(source),
                        "-o",
                        str(temporary_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                temporary_path.replace(library_path)
            except (OSError, subprocess.CalledProcessError) as error:
                temporary_path.unlink(missing_ok=True)
                detail = getattr(error, "stderr", "")
                raise NativeSearchUnavailable(
                    f"Falha ao compilar Dijkstra nativo: {detail or error}"
                ) from error

    library = ctypes.CDLL(str(library_path))
    uint64_pointer = ctypes.POINTER(ctypes.c_uint64)
    uint32_pointer = ctypes.POINTER(ctypes.c_uint32)
    int64_pointer = ctypes.POINTER(ctypes.c_int64)
    double_pointer = ctypes.POINTER(ctypes.c_double)
    int32_pointer = ctypes.POINTER(ctypes.c_int32)
    library.mt_dijkstra_source.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        uint64_pointer,
        uint32_pointer,
        int64_pointer,
        double_pointer,
        int32_pointer,
        uint64_pointer,
        int64_pointer,
        uint64_pointer,
        int64_pointer,
        uint32_pointer,
        ctypes.c_uint32,
        ctypes.POINTER(_NativeResult),
    ]
    library.mt_dijkstra_source.restype = ctypes.c_int
    library.mt_dijkstra_free.argtypes = [ctypes.POINTER(_NativeResult)]
    library.mt_dijkstra_free.restype = None
    return library


def _native_compiler_details() -> dict[str, object]:
    compiler = shutil.which("g++")
    if compiler is None:
        raise NativeSearchUnavailable("g++ não está instalado")
    source = Path(__file__).with_name("fast_dijkstra.cpp")
    try:
        version = subprocess.run(
            [compiler, "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()[0]
    except (OSError, subprocess.CalledProcessError, IndexError):
        version = "unknown"
    return {
        "compiler": compiler,
        "version": version,
        "flags": list(_COMPILER_FLAGS),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


class FastDijkstra:
    """Compact, threaded C++ Dijkstra engine with Python-owned graph inputs."""

    def __init__(
        self,
        library: ctypes.CDLL,
        *,
        compiler_details: dict[str, object],
        node_count: int,
        terminal_indexes: dict[int, int],
        offsets: array,
        edge_targets: array,
        edge_ways: array,
        distance_weights: array,
        time_weights: array,
        incoming_rule_indexes: array,
        only_offsets: array,
        only_ways: array,
        forbidden_offsets: array,
        forbidden_ways: array,
        edge_ids: list[str],
    ) -> None:
        self._library = library
        self.compiler_details = compiler_details
        self.node_count = node_count
        self.edge_count = len(edge_targets)
        self.terminal_indexes = terminal_indexes
        self._offsets = offsets
        self._edge_targets = edge_targets
        self._edge_ways = edge_ways
        self._distance_weights = distance_weights
        self._time_weights = time_weights
        self._incoming_rule_indexes = incoming_rule_indexes
        self._only_offsets = only_offsets
        self._only_ways = only_ways
        self._forbidden_offsets = forbidden_offsets
        self._forbidden_ways = forbidden_ways
        self._edge_ids = edge_ids

    @classmethod
    def from_network(
        cls,
        network: RoadNetwork,
        terminal_node_ids: set[int],
    ) -> FastDijkstra:
        library = _load_native_library()
        compiler_details = _native_compiler_details()
        node_ids = sorted(network.nodes)
        if len(node_ids) >= 2**32 or len(network.edges) >= 2**32:
            raise NativeSearchUnavailable("Grafo excede os índices de 32 bits do acelerador")
        node_indexes = {node_id: index for index, node_id in enumerate(node_ids)}
        terminal_indexes = {
            node_id: node_indexes[node_id]
            for node_id in terminal_node_ids
            if node_id in node_indexes
        }
        if len(terminal_indexes) != len(terminal_node_ids):
            raise KeyError("Sede encaixada referencia nó ausente no grafo")

        rule_indexes: dict[tuple[int, int], int] = {}
        only_offsets = array("Q", [0])
        only_ways = array("q")
        forbidden_offsets = array("Q", [0])
        forbidden_ways = array("q")
        for key in sorted(network._turn_rules):
            only, forbidden = network._turn_rules[key]
            rule_indexes[key] = len(rule_indexes)
            only_ways.extend(sorted(only))
            only_offsets.append(len(only_ways))
            forbidden_ways.extend(sorted(forbidden))
            forbidden_offsets.append(len(forbidden_ways))

        offsets = array("Q", [0])
        edge_targets = array("I")
        edge_ways = array("q")
        distance_weights = array("d")
        time_weights = array("d")
        incoming_rule_indexes = array("i")
        edge_ids: list[str] = []
        for node_id in node_ids:
            for edge in network.adjacency[node_id]:
                edge_targets.append(node_indexes[edge.target])
                edge_ways.append(edge.way_id)
                distance_weights.append(edge.length_m)
                time_weights.append(edge.travel_time_s)
                incoming_rule_indexes.append(
                    rule_indexes.get((edge.way_id, edge.target), -1)
                )
                edge_ids.append(edge.edge_id)
            offsets.append(len(edge_targets))

        del node_indexes, node_ids, rule_indexes
        gc.collect()
        return cls(
            library,
            compiler_details=compiler_details,
            node_count=len(offsets) - 1,
            terminal_indexes=terminal_indexes,
            offsets=offsets,
            edge_targets=edge_targets,
            edge_ways=edge_ways,
            distance_weights=distance_weights,
            time_weights=time_weights,
            incoming_rule_indexes=incoming_rule_indexes,
            only_offsets=only_offsets,
            only_ways=only_ways,
            forbidden_offsets=forbidden_offsets,
            forbidden_ways=forbidden_ways,
            edge_ids=edge_ids,
        )

    def search_source(
        self,
        source_node_id: int,
        target_node_ids: list[int],
        metric: PathMetric,
    ) -> tuple[list[float | None], list[list[str] | None]]:
        source = self.terminal_indexes[source_node_id]
        target_indexes = array(
            "I", (self.terminal_indexes[node_id] for node_id in target_node_ids)
        )
        weights = self._distance_weights if metric == "distance" else self._time_weights
        result = _NativeResult()
        status = self._library.mt_dijkstra_source(
            self.node_count,
            self.edge_count,
            source,
            _buffer_pointer(self._offsets, ctypes.c_uint64),
            _buffer_pointer(self._edge_targets, ctypes.c_uint32),
            _buffer_pointer(self._edge_ways, ctypes.c_int64),
            _buffer_pointer(weights, ctypes.c_double),
            _buffer_pointer(self._incoming_rule_indexes, ctypes.c_int32),
            _buffer_pointer(self._only_offsets, ctypes.c_uint64),
            _buffer_pointer(self._only_ways, ctypes.c_int64),
            _buffer_pointer(self._forbidden_offsets, ctypes.c_uint64),
            _buffer_pointer(self._forbidden_ways, ctypes.c_int64),
            _buffer_pointer(target_indexes, ctypes.c_uint32),
            len(target_indexes),
            ctypes.byref(result),
        )
        if status:
            raise RuntimeError(f"Dijkstra nativo falhou; código interno {status}")

        try:
            costs: list[float | None] = []
            paths: list[list[str] | None] = []
            for target_index in range(len(target_indexes)):
                cost = result.costs[target_index]
                if not (cost < float("inf")):
                    costs.append(None)
                    paths.append(None)
                    continue
                costs.append(cost)
                start = result.path_offsets[target_index]
                end = result.path_offsets[target_index + 1]
                paths.append(
                    [
                        self._edge_ids[result.edge_indexes[index]]
                        for index in range(start, end)
                    ]
                )
            return costs, paths
        finally:
            self._library.mt_dijkstra_free(ctypes.byref(result))
