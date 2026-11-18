"""Conservative structural cone; never a semantic impact label."""
from collections import defaultdict


def structural_cone(graph, seeds):
    adjacency = defaultdict(set)
    for edge in graph['edges']:
        src, dst, relation = edge['source'], edge['target'], edge['relation']
        if relation in ('reads', 'depends-on'):
            adjacency[dst].add(src)
        elif relation in ('writes', 'implemented-by', 'contains'):
            adjacency[src].add(dst)
        if relation == 'contains':
            adjacency[dst].add(src)
    visited, pending = set(seeds), list(seeds)
    while pending:
        for nxt in adjacency[pending.pop()]:
            if nxt not in visited:
                visited.add(nxt)
                pending.append(nxt)
    return dict(nodes=sorted(visited), kind='structural_cone',
                complete=graph['coverage']['dependency_complete'],
                semantic_impact=None, absence_proves_no_impact=False)
