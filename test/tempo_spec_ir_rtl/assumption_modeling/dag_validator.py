"""Structural, domain and provenance checks for local design-decision DAGs."""


def validate_dag(dag, requirement_ids=()):
    errors, nodes = [], {}
    known_requirements = set(requirement_ids)
    for node in dag.get('nodes', []):
        identifier = node.get('id')
        if not identifier or identifier in nodes:
            errors.append('node IDs must be unique and nonempty')
        nodes[identifier] = node
        if any(r not in known_requirements for r in node.get('requirement_ids', [])):
            errors.append(f'{identifier}: invalid requirement reference')
    adjacency = {identifier: [] for identifier in nodes}
    for edge in dag.get('edges', []):
        src, dst = edge.get('source'), edge.get('target')
        if src not in nodes or dst not in nodes:
            errors.append('invalid edge reference')
            continue
        adjacency[src].append(dst)
    for root in dag.get('roots', []):
        if root not in nodes:
            errors.append('invalid root reference')
    for identifier, node in nodes.items():
        if node.get('kind') != 'decision':
            continue
        domain = node.get('value_domain', [])
        values = [e.get('value') for e in dag.get('edges', []) if e.get('source') == identifier]
        if not isinstance(domain, list) or not domain or any(type(v) is not str or v == 'unknown' for v in domain) or len(set(domain)) != len(domain):
            errors.append(f'{identifier}: invalid typed domain')
            continue
        if any(type(v) is not str for v in values):
            errors.append(f'{identifier}: invalid typed branch value')
            continue
        if len(set(values)) != len(values) or any(v not in domain for v in values):
            errors.append(f'{identifier}: invalid or overlapping branches')
        if node.get('domain_closed') and set(values) != set(domain):
            errors.append(f'{identifier}: incomplete closed domain')
        if not node.get('domain_closed'):
            exits = [e for e in dag.get('edges', []) if e.get('source') == identifier and e.get('value') == 'other']
            if not exits or not any(nodes.get(e['target'], {}).get('kind') == 'requires_refinement' for e in exits):
                errors.append(f'{identifier}: open domain needs refinement exit')
        if node.get('selected_value') is not None and node['selected_value'] not in domain:
            errors.append(f'{identifier}: selected value outside domain')
    # Iterative Kahn traversal; no recursion-depth failure for large DAGs.
    indegree = dict.fromkeys(nodes, 0)
    for targets in adjacency.values():
        for target in targets:
            indegree[target] += 1
    ready = [n for n, degree in indegree.items() if degree == 0]
    count = 0
    while ready:
        node = ready.pop()
        count += 1
        for target in adjacency[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if count != len(nodes):
        errors.append('cycle in condition DAG')
    return dict(status='valid' if not errors else 'invalid', errors=errors)
