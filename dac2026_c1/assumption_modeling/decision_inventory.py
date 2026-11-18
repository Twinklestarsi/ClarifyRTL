"""Enumerate and locate the supported design-choice fields from current IR.

Coverage is deliberately scoped to scalar reset values and sequential action
expressions. It says nothing about assumptions outside this inventory.
"""


def decision_key(a):
    if a['kind']=='reset_value':return ('reset_value',a['target'])
    if a['kind']=='state_update':return ('state_update',a['transition'],int(a['action_index']))
    return None


def supported_candidates(assumptions):
    groups={}
    for a in assumptions:
        key=decision_key(a)
        if key is not None:groups.setdefault(key,[]).append(a)
    # Multiple source clauses may refer to the same current action. Present
    # one choice per field, preferring the implementation-derived candidate.
    return [min(items,key=lambda a:(not a['id'].startswith('A:rule:'),a['id']))
            for _,items in sorted(groups.items())]


def ir_inventory(model):
    result=[]
    for state in model.states:
        domain=model.domain_map().get(state.clock_domain)
        if domain and domain.reset and state.type not in ('memory','ram'):
            result.append(dict(key=('reset_value',state.name),kind='reset_value',target=state.name,
                ir_node=f'IR:state:{state.name}',ir_field=f'states[name={state.name}].reset_value'))
    for t in model.transitions:
        if t.transition_type!='sequential':continue
        for i,a in enumerate(t.actions):
            if a.target in model.state_map() and model.state_map()[a.target].type not in ('memory','ram'):
                result.append(dict(key=('state_update',t.name,i),kind='state_update',target=a.target,
                    ir_node=f'IR:action:{t.name}:{i}',ir_field=f'transitions[name={t.name}].action[{i}].expression'))
    return result


def choice_locations(graph,a):
    nodes={n['id']:n for n in graph['nodes']}
    if a['kind']=='reset_value':
        seeds={f"IR:state:{a['target']}"};role='reset_override'
        field=f"states[name={a['target']}].reset_value"
    elif a['kind']=='state_update':
        seeds={f"IR:action:{a['transition']}:{a['action_index']}"};role='action'
        field=f"transitions[name={a['transition']}].action[{a['action_index']}].expression"
    else:return dict(ir_fields=[],ir_nodes=[],rtl_regions=[],complete=False)
    regions=[]
    for e in graph['edges']:
        if e['source'] in seeds and e['relation']=='implemented-by':
            node=nodes[e['target']]
            if node['type']=='RTL' and node['subtype']==role:
                attrs=node['attributes'];regions.append(dict(node_id=node['id'],
                    line_start=attrs['line_start'],line_end=attrs['line_end'],precision=attrs.get('precision')))
    return dict(ir_fields=[field],ir_nodes=sorted(seeds & nodes.keys()),rtl_regions=regions,
                complete=bool(regions) and seeds.issubset(nodes),
                meaning='current implementation field; affected downstream nodes are reported separately')
