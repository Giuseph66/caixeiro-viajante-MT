#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <queue>
#include <unordered_map>
#include <vector>

namespace {
constexpr std::uint32_t kNoEdge = std::numeric_limits<std::uint32_t>::max();

struct QueueEntry {
    double cost;
    std::uint32_t node;
    std::uint32_t edge;
};

struct QueueEntryGreater {
    bool operator()(const QueueEntry& left, const QueueEntry& right) const {
        if (left.cost != right.cost) return left.cost > right.cost;
        if (left.node != right.node) return left.node > right.node;
        return left.edge > right.edge;
    }
};

bool turn_allowed(
    std::int32_t rule,
    std::int64_t outgoing_way,
    const std::uint64_t* only_offsets,
    const std::int64_t* only_ways,
    const std::uint64_t* forbidden_offsets,
    const std::int64_t* forbidden_ways
) {
    if (rule < 0) return true;

    const auto rule_index = static_cast<std::uint32_t>(rule);
    for (auto index = forbidden_offsets[rule_index];
         index < forbidden_offsets[rule_index + 1];
         ++index) {
        if (forbidden_ways[index] == outgoing_way) return false;
    }

    const auto begin = only_offsets[rule_index];
    const auto end = only_offsets[rule_index + 1];
    if (begin == end) return true;
    for (auto index = begin; index < end; ++index) {
        if (only_ways[index] == outgoing_way) return true;
    }
    return false;
}
}  // namespace

extern "C" {
struct MtDijkstraResult {
    double* costs;
    std::uint64_t* path_offsets;
    std::uint32_t* edge_indexes;
    std::uint64_t edge_count;
};

int mt_dijkstra_source(
    std::uint32_t node_count,
    std::uint32_t edge_count,
    std::uint32_t source,
    const std::uint64_t* offsets,
    const std::uint32_t* targets,
    const std::int64_t* edge_ways,
    const double* weights,
    const std::int32_t* incoming_rule_by_edge,
    const std::uint64_t* only_offsets,
    const std::int64_t* only_ways,
    const std::uint64_t* forbidden_offsets,
    const std::int64_t* forbidden_ways,
    const std::uint32_t* target_nodes,
    std::uint32_t target_count,
    MtDijkstraResult* result
) {
    if (!result || source >= node_count || !offsets || !targets || !edge_ways || !weights ||
        !incoming_rule_by_edge || !only_offsets || !forbidden_offsets ||
        (target_count && !target_nodes)) {
        return 1;
    }

    result->costs = nullptr;
    result->path_offsets = nullptr;
    result->edge_indexes = nullptr;
    result->edge_count = 0;

    try {
        const auto infinity = std::numeric_limits<double>::infinity();
        std::vector<double> distances(edge_count, infinity);
        std::vector<std::uint32_t> previous(edge_count, kNoEdge);
        std::vector<std::uint32_t> final_edges(target_count, kNoEdge);
        std::vector<double> target_costs(target_count, infinity);
        std::vector<bool> settled(target_count, false);
        std::unordered_map<std::uint32_t, std::uint32_t> target_indexes;
        target_indexes.reserve(target_count * 2 + 1);
        for (std::uint32_t index = 0; index < target_count; ++index) {
            if (target_nodes[index] >= node_count ||
                !target_indexes.emplace(target_nodes[index], index).second) {
                return 2;
            }
        }

        std::priority_queue<QueueEntry, std::vector<QueueEntry>, QueueEntryGreater> queue;
        queue.push({0.0, source, kNoEdge});
        std::uint32_t pending = target_count;
        while (!queue.empty() && pending) {
            const auto current = queue.top();
            queue.pop();
            if (current.edge != kNoEdge && current.cost != distances[current.edge]) continue;

            const auto target = target_indexes.find(current.node);
            if (target != target_indexes.end() && !settled[target->second]) {
                settled[target->second] = true;
                final_edges[target->second] = current.edge;
                target_costs[target->second] = current.cost;
                --pending;
            }

            const auto incoming_rule = current.edge == kNoEdge
                ? -1
                : incoming_rule_by_edge[current.edge];
            for (auto edge = offsets[current.node]; edge < offsets[current.node + 1]; ++edge) {
                const auto edge_index = static_cast<std::uint32_t>(edge);
                if (current.edge != kNoEdge &&
                    !turn_allowed(
                        incoming_rule,
                        edge_ways[edge_index],
                        only_offsets,
                        only_ways,
                        forbidden_offsets,
                        forbidden_ways)) {
                    continue;
                }

                const auto next_cost = current.cost + weights[edge_index];
                if (next_cost >= distances[edge_index]) continue;
                distances[edge_index] = next_cost;
                previous[edge_index] = current.edge;
                queue.push({next_cost, targets[edge_index], edge_index});
            }
        }

        std::vector<std::uint32_t> path_indexes;
        std::vector<std::uint64_t> path_offsets(target_count + 1, 0);
        for (std::uint32_t index = 0; index < target_count; ++index) {
            path_offsets[index] = path_indexes.size();
            auto edge = final_edges[index];
            if (edge != kNoEdge) {
                std::vector<std::uint32_t> reversed;
                while (edge != kNoEdge) {
                    reversed.push_back(edge);
                    if (reversed.size() > edge_count) return 3;
                    edge = previous[edge];
                }
                path_indexes.insert(path_indexes.end(), reversed.rbegin(), reversed.rend());
            }
        }
        path_offsets[target_count] = path_indexes.size();

        result->costs = static_cast<double*>(std::malloc(sizeof(double) * target_count));
        result->path_offsets = static_cast<std::uint64_t*>(
            std::malloc(sizeof(std::uint64_t) * (target_count + 1)));
        if (path_indexes.size()) {
            result->edge_indexes = static_cast<std::uint32_t*>(
                std::malloc(sizeof(std::uint32_t) * path_indexes.size()));
        }
        if ((target_count && !result->costs) || !result->path_offsets ||
            (!path_indexes.empty() && !result->edge_indexes)) {
            std::free(result->costs);
            std::free(result->path_offsets);
            std::free(result->edge_indexes);
            result->costs = nullptr;
            result->path_offsets = nullptr;
            result->edge_indexes = nullptr;
            return 4;
        }
        std::copy(target_costs.begin(), target_costs.end(), result->costs);
        std::copy(path_offsets.begin(), path_offsets.end(), result->path_offsets);
        if (!path_indexes.empty()) {
            std::copy(path_indexes.begin(), path_indexes.end(), result->edge_indexes);
        }
        result->edge_count = path_indexes.size();
        return 0;
    } catch (...) {
        return 5;
    }
}

void mt_dijkstra_free(MtDijkstraResult* result) {
    if (!result) return;
    std::free(result->costs);
    std::free(result->path_offsets);
    std::free(result->edge_indexes);
    result->costs = nullptr;
    result->path_offsets = nullptr;
    result->edge_indexes = nullptr;
    result->edge_count = 0;
}
}
