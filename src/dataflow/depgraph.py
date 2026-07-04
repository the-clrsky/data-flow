"""Generic dependency-ordered topological sort (DFS-based, cycle-tolerant).

Used to order cross-table operations - e.g. recreating foreign keys so a
parent table's PK/unique constraint is handled before a child's FK
referencing it - without hard-failing on cycles that real-world schemas can
have (self-references, mutual FKs).
"""


def topo_sort(nodes, edges):
    """`edges` is an iterable of (dependent, dependency) pairs meaning
    `dependent` must come after `dependency` in the result. Nodes not
    mentioned in any edge keep their relative input order. Cycles are
    broken arbitrarily rather than raising, since a perfect order isn't
    always possible.
    """
    nodes = list(dict.fromkeys(nodes))
    node_set = set(nodes)
    deps = {n: [] for n in nodes}
    for dependent, dependency in edges:
        if dependent in node_set and dependency in node_set and dependent != dependency:
            deps[dependent].append(dependency)

    ordered = []
    done = set()

    def visit(n, path):
        if n in done or n in path:
            return
        path.add(n)
        for dep in deps[n]:
            visit(dep, path)
        path.discard(n)
        done.add(n)
        ordered.append(n)

    for n in nodes:
        visit(n, set())
    return ordered
