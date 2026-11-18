"""Query-relative features computed exclusively from the exported visible graph.

These are syntactic comparisons and dependency distances, not bounded proofs.
No paths, case IDs, oracle labels, measurements or future evidence are accepted.
The original frozen 402-dimensional node representation is unchanged.
"""
from collections import defaultdict, deque
import math
import re

from .expressions import parse, Unsupported
from .schema import digest

CONTEXT_VERSION = 'c1-decision-context-1'
QUERY_NAMES = (
    'reset_choice', 'update_choice', 'verified_binding', 'source_present',
    'source_exact_match', 'source_syntactic_difference', 'explicit_allowed_set',
    'current_in_allowed_set', 'current_outside_allowed_set', 'source_conflict',
    'target_is_output', 'target_width', 'target_count', 'normal_guard_is_constant',
    'log_nodes', 'log_edges', 'log_states', 'log_state_bits', 'log_max_width',
    'log_ir_nodes', 'log_rtl_nodes', 'log_output_count', 'log_expression_chars',
)
ALT_NAMES = QUERY_NAMES + (
    'hold_alternative', 'toggle_alternative', 'local_sat', 'local_unsat',
    'local_unknown', 'local_missing', 'log_cone_nodes', 'log_cone_outputs',
    'inverse_output_distance', 'output_unreachable_structurally',
)
SUBTYPES = ('state', 'port', 'derived', 'transition', 'guard', 'action',
            'else_action', 'schedule', 'reset', 'clock', 'port_declaration',
            'state_declaration', 'next_declaration', 'default_hold',
            'effective_branch', 'reset_override', 'state_commit',
            'derived_assignment', 'derived_declaration', 'reset_condition',
            'clock_event')
NODE_NAMES = ('seed', 'in_cone', 'inverse_distance', 'output', 'input',
              'same_target_state', 'mapped_from_seed', 'local_unsat') + tuple('role_' + x for x in SUBTYPES)


def _expression(value):
    try:
        ast = parse(str(value))
        if ast.op in ('const', 'unsized'):
            return ('number', ast.args[0])
        return ast
    except (Unsupported, ValueError, TypeError):
        return None


def _source_comparisons(graph, query, bound, nodes):
    """Match a canonical clause to this target and guard, never by class label."""
    kind = query.get('kind', nodes[query['id']].get('subtype'))
    current = _expression(nodes[query['id']]['text'])
    targets = {nodes[n]['text'] for n in bound if nodes[n]['subtype'] == 'state'}
    guards = {_expression(nodes[n]['text']) for n in bound if nodes[n]['subtype'] == 'guard'} - {None}
    for e in graph['edges']:
        if e['source'] in bound and e['relation'] == 'writes':
            targets.add(nodes[e['target']]['text'])
    claims = []
    for node in graph['nodes']:
        if node['type'] != 'Requirement':
            continue
        attrs = node.get('attributes', {})
        if attrs.get('interpretation_status') in ('canonical_verified', 'reviewed'):
            claims.extend(attrs.get('claims') or [])
        # This canonical allowed-expression sentence was not supported by the
        # original extractor. Match its target AND trigger; retain the raw text.
        m = re.fullmatch(r'When (.+), the update of (\w+) may use either \((.+)\) or \((.+)\); both are explicitly allowed\.?', node['text'])
        if m and m[2] in targets and _expression(m[1]) in guards:
            claims.append(dict(kind='implementation_freedom', choice_kind='state_update',
                               target=m[2], trigger=m[1], allowed_values=[m[3], m[4]]))
    normal = []; allowed = []
    for c in claims:
        ck = c.get('choice_kind') if c.get('kind') == 'implementation_freedom' else c.get('kind')
        if ck != kind or c.get('target') not in targets:
            continue
        if kind == 'state_update' and _expression(c.get('trigger')) not in guards:
            continue
        if c.get('kind') == 'implementation_freedom':
            values = [_expression(v) for v in c.get('allowed_values', [])]
            if current is not None and values and all(v is not None for v in values):
                allowed.append(current in values)
        else:
            expected = _expression(c.get('expression') if kind == 'state_update' else c.get('value'))
            if current is not None and expected is not None:
                normal.append(current == expected)
    return [float(bool(normal)), float(any(normal)), float(any(not v for v in normal)),
            float(bool(allowed)), float(any(allowed)), float(any(not v for v in allowed)),
            float((bool(normal) and len(set(normal)) > 1) or (bool(allowed) and len(set(allowed)) > 1))]


def add_context(features, graph, queries, *, compact=False):
    """Return a new cache with deterministic supplementary input features."""
    if graph.get('snapshot_id') not in ('post_local_witness', 'post_rtl_pre_validation'):
        raise ValueError('decision context requires a visible G0/G1 snapshot')
    nodes = {n['id']: n for n in graph['nodes']}
    if features['node_ids'] != list(nodes):
        raise ValueError('context node ordering mismatch')
    adjacency = defaultdict(set); implemented = defaultdict(set)
    for e in graph['edges']:
        src, dst, relation = e['source'], e['target'], e['relation']
        if relation in ('reads', 'depends-on'):
            adjacency[dst].add(src)
        if relation in ('writes', 'implemented-by', 'contains'):
            adjacency[src].add(dst)
        if relation == 'contains':
            adjacency[dst].add(src)
        if relation == 'implemented-by':
            implemented[src].add(dst)
    states = [n for n in nodes.values() if n['type'] == 'IR' and n['subtype'] == 'state']
    outputs = {n['id'] for n in nodes.values() if 'output' in n.get('attributes', {}).get('roles', [])}
    widths = [float(n.get('attributes', {}).get('width') or 0) for n in states]
    global_context = [math.log1p(v) for v in (
        len(nodes), len(graph['edges']), len(states), sum(widths), max(widths, default=0),
        sum(n['type'] == 'IR' for n in nodes.values()), sum(n['type'] == 'RTL' for n in nodes.values()),
        len(outputs), sum(len(n['text']) for n in nodes.values() if n['type'] == 'IR'))]
    context = {}
    for q in queries:
        aid = q['id']; kind = q.get('kind', nodes[aid].get('subtype'))
        bound = {e['target'] for e in graph['edges'] if e['source'] == aid and e['relation'] == 'binds-to'}
        # A reset value edits the state initializer, not the global reset signal.
        # An assignment expression edits the action, not its enable condition.
        seeds = {n for n in bound if nodes[n]['subtype'] in (('state',) if kind == 'reset_value' else ('action', 'else_action'))}
        target_states = set(n for n in seeds if nodes[n]['subtype'] == 'state')
        target_states.update(e['target'] for e in graph['edges'] if e['source'] in seeds and e['relation'] == 'writes')
        distance = {n: 0 for n in seeds}; queue = deque(seeds)
        while queue:
            n = queue.popleft()
            for other in adjacency[n]:
                if other not in distance:
                    distance[other] = distance[n] + 1; queue.append(other)
        source = _source_comparisons(graph, q, bound, nodes)
        guards = [nodes[n]['text'] for n in bound if nodes[n]['subtype'] == 'guard']
        guard_constant = bool(guards) and all(isinstance(_expression(g), tuple) for g in guards)
        qvalues = [float(kind == 'reset_value'), float(kind == 'state_update'),
                   float(nodes[aid]['attributes'].get('binding_state') == 'verified')] + source + [
            float(bool(target_states & outputs)),
            math.log1p(max((nodes[n].get('attributes', {}).get('width') or 0 for n in target_states), default=0)),
            math.log1p(len(target_states)), float(guard_constant)] + global_context
        alternatives = {}
        for alt in q['alternatives']:
            statuses = {n['attributes'].get('status') for n in nodes.values() if n['type'] == 'Evidence'
                        and n['attributes'].get('assumption_id') == aid
                        and n['attributes'].get('alternative_id') == alt['id']
                        and n['attributes'].get('claim_kind') == 'local_next_state_difference'
                        and n['attributes'].get('level') == 'local'}
            unsat = statuses == {'unsat'}
            seen_outputs = outputs & distance.keys()
            avals = qvalues + [float(alt.get('value') == 'hold_current_state'),
                float(alt.get('value') == 'toggle_low_result_bit'), float(statuses == {'sat'}),
                float(unsat), float(bool(statuses) and statuses not in ({'sat'}, {'unsat'})),
                float(not statuses), math.log1p(len(distance)), math.log1p(len(seen_outputs)),
                1 / (1 + min(distance[n] for n in seen_outputs)) if seen_outputs else 0., float(not seen_outputs)]
            mapped = set().union(*(implemented[n] for n in seeds)) if seeds else set()
            nvals = []
            for n in nodes.values():
                nid = n['id']; roles = n.get('attributes', {}).get('roles', [])
                nvals.append([float(nid in seeds), float(nid in distance),
                    1 / (1 + distance[nid]) if nid in distance else 0., float('output' in roles),
                    float('input' in roles), float(nid in target_states), float(nid in mapped), float(unsat)]
                    + [float(n['subtype'] == s) for s in SUBTYPES])
            alternatives[alt['id']] = dict(context=avals, nodes=nvals)
        if compact and alternatives:
            # Node relationships are shared by all alternatives of this query;
            # only the local-unsat column can vary between alternatives.
            common=next(iter(alternatives.values()))['nodes']
            common=[row[:7]+[0.]+row[8:] for row in common]
            for item in alternatives.values():item.pop('nodes')
            context[aid] = dict(context=qvalues, alternatives=alternatives, nodes=common)
        else:
            context[aid] = dict(context=qvalues, alternatives=alternatives)
    result = dict(features, decision_context_schema=CONTEXT_VERSION, decision_context=context,
                  decision_context_names=dict(query=list(QUERY_NAMES), alternative=list(ALT_NAMES), node=list(NODE_NAMES)))
    result.pop('feature_hash', None); result['feature_hash'] = digest(result)
    return result


def node_context(query_context, alternative_id):
    import numpy as np
    alt=query_context['alternatives'][alternative_id]
    if 'nodes' in alt:return np.asarray(alt['nodes'])
    values=np.asarray(query_context['nodes']).copy()
    values[:,NODE_NAMES.index('local_unsat')]=alt['context'][ALT_NAMES.index('local_unsat')]
    return values
