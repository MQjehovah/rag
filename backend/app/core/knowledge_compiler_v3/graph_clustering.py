"""图聚类算法（Louvain / 连通分量），无任何 Card/KO 依赖，且完全确定性。

从 community.py 提取出来的纯函数，供旧 Card 主链（P5/P19）与新的
Page/Chunk/Evidence 主链（Phase D-3）共用。本模块不 import 任何数据库模型。

确定性保证（跨进程 / 跨 PYTHONHASHSEED 一致）：
- 输入边先 sorted；
- 节点遍历、邻居 Community 遍历均 sorted；
- 相同 gain / 相同权重使用字典序 tie-break；
- 输出 cluster 列表按成员字典序排序。
"""
from __future__ import annotations


def _sort_clusters(clusters: list[set[str]]) -> list[set[str]]:
    """按成员字典序稳定排序，保证输出顺序跨进程一致。"""
    return sorted(clusters, key=lambda c: tuple(sorted(c)))


def _connected_components(
    entity_ids: set[str],
    edges: list[tuple[str, str]],
) -> list[set[str]]:
    """并查集求连通分量（确定性）。"""
    parent = {e: e for e in entity_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for a, b in sorted(edges):
        if a in parent and b in parent:
            union(a, b)

    groups: dict[str, set[str]] = {}
    for e in sorted(entity_ids):
        groups.setdefault(find(e), set()).add(e)
    return _sort_clusters(list(groups.values()))


def louvain_partition(
    entity_ids: set[str],
    weighted_edges: list[tuple[str, str, float]],
) -> list[set[str]]:
    """纯 Python 加权单层 Louvain（无聚合收缩阶段），无 networkx 依赖，确定性。

    说明：本实现只做一层节点局部移动（modularity gain 贪心），不做社区
    收缩为超级节点后的多轮聚合。诚实声明为「单层 Louvain」，而非完整
    多层级 Louvain。
    """
    nodes = sorted(entity_ids)
    if not nodes:
        return []
    # 先稳定排序边，保证 adj 插入序与后续浮点求和顺序跨进程一致
    weighted_edges = sorted(weighted_edges)
    adj: dict[str, dict[str, float]] = {n: {} for n in nodes}
    for a, b, weight in weighted_edges:
        if a == b or a not in adj or b not in adj or weight <= 0:
            continue
        adj[a][b] = adj[a].get(b, 0.0) + weight
        adj[b][a] = adj[b].get(a, 0.0) + weight
    m = sum(sum(weights.values()) for weights in adj.values()) / 2.0
    if m <= 0:
        return _sort_clusters([{n} for n in nodes])

    community = {n: n for n in nodes}

    def degree(node: str) -> float:
        return sum(adj[node].values())

    def comm_total(cid: str) -> float:
        return sum(degree(n) for n in nodes if community[n] == cid)

    def ki_in(node: str, cid: str) -> float:
        return sum(w for nb, w in adj[node].items() if community[nb] == cid)

    improved = True
    while improved:
        improved = False
        for node in nodes:
            current = community[node]
            k_i = degree(node)
            best = current
            best_gain = 0.0
            # 邻居 Community 遍历用 sorted，相同 gain 时字典序 tie-break
            neighbor_comms = sorted({community[nb] for nb in adj[node]} | {current})
            community[node] = f"__empty_{node}"
            for cid in neighbor_comms:
                gain = (ki_in(node, cid) / m) - (comm_total(cid) * k_i / (2 * m * m))
                if gain > best_gain + 1e-12:
                    best_gain = gain
                    best = cid
            community[node] = best if best_gain > 0 else current
            if community[node] != current:
                improved = True

    groups: dict[str, set[str]] = {}
    for node in nodes:
        groups.setdefault(community[node], set()).add(node)
    return _sort_clusters(list(groups.values()))


def _merge_and_split(
    clusters: list[set[str]],
    weighted_edges: list[tuple[str, str, float]],
    large_limit: int,
) -> list[set[str]]:
    """单实体并入最强邻居；超大社区用高权重边再切连通分量（确定性）。"""
    weighted_edges = sorted(weighted_edges)
    weight_by_pair: dict[tuple[str, str], float] = {}
    for a, b, w in weighted_edges:
        if a == b:
            continue
        key = tuple(sorted((a, b)))
        weight_by_pair[key] = weight_by_pair.get(key, 0.0) + w

    clusters = _sort_clusters(list(clusters))
    cluster_of = {e: i for i, cluster in enumerate(clusters) for e in sorted(cluster)}

    # 单实体：若存在跨社区边，并入权重最高的邻居社区（相同权重字典序 tie-break）
    changed = True
    while changed:
        changed = False
        for idx, cluster in enumerate(list(clusters)):
            if len(cluster) != 1:
                continue
            entity = next(iter(cluster))
            best_idx = None
            best_w = 0.0
            for (a, b), w in sorted(weight_by_pair.items()):
                other = b if a == entity else a if b == entity else None
                if other is None or other not in cluster_of:
                    continue
                other_idx = cluster_of[other]
                if other_idx == idx:
                    continue
                if w > best_w:
                    best_w = w
                    best_idx = other_idx
            if best_idx is None:
                continue
            clusters[best_idx].add(entity)
            clusters[idx] = set()
            cluster_of[entity] = best_idx
            changed = True
    clusters = _sort_clusters([c for c in clusters if c])

    # 超大社区：用高权重边再切连通分量
    split: list[set[str]] = []
    for cluster in clusters:
        if len(cluster) <= large_limit:
            split.append(cluster)
            continue
        weights = sorted(
            w for (a, b), w in weight_by_pair.items()
            if a in cluster and b in cluster
        )
        if not weights:
            split.append(cluster)
            continue
        threshold = weights[len(weights) // 2]
        strong = sorted(
            (a, b) for (a, b), w in weight_by_pair.items()
            if a in cluster and b in cluster and w >= threshold
        )
        parts = _connected_components(cluster, strong)
        split.extend(parts if len(parts) > 1 else [cluster])
    return _sort_clusters(split)
