"""Block 4: IR dependency graph and question graph construction."""

from .graph import (adjacency, anchor_to_nodes, build_ir_graph, graph_stats,
                    output_nodes, reachable, resolve_anchor_nodes, to_mermaid)

__all__ = ["adjacency", "anchor_to_nodes", "build_ir_graph", "graph_stats",
           "output_nodes", "reachable", "resolve_anchor_nodes", "to_mermaid"]
