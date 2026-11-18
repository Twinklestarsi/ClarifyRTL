"""Audited IR/RTL observation bindings. Mapping is not itself a positive impact label."""


def node_observation(node, graph, model):
    identifier=node['id'];kind=node['type'];subtype=node.get('subtype')
    if kind=='IR':
        if subtype=='state':return 'state:'+identifier.split(':',2)[2]
        if subtype=='guard':return 'guard:'+identifier.split(':',2)[2]
        if subtype=='action':return 'action:'+identifier.split(':',2)[2]
        if subtype=='schedule':return 'next:'+identifier.split(':',2)[2]
        if subtype=='reset':return 'reset:'+identifier.split(':',2)[2]
        return None
    if kind!='RTL':return None
    sources=[e['source'] for e in graph['edges'] if e['relation']=='implemented-by' and e['target']==identifier]
    def source(prefix):return next((s[len(prefix):] for s in sources if s.startswith(prefix)),None)
    target=source('IR:schedule:') or source('IR:state:')
    if subtype=='effective_branch':
        transition=source('IR:guard:')
        return f'effective:{transition}:{target}' if transition and target else None
    if subtype=='action':
        action=source('IR:action:')
        return 'action:'+action if action else None
    if subtype=='state_commit':return 'next:'+target if target else None
    if subtype=='default_hold':return 'state:'+target if target else None
    if subtype=='reset_override':return 'reset_update:'+target if target else None
    if subtype=='reset_condition':
        domain=source('IR:reset:')
        return 'reset:'+domain if domain else None
    # Declarations/clock/initialization/opaque statements have no unique bounded observable here.
    return None


def impact_labels(graph, model, bounded_result, binding_graph=None):
    queries={q['observable']:q for q in bounded_result.get('queries',[])}
    result={}
    quarantined=bounded_result.get('status')=='quarantined'
    for node in graph['nodes']:
        observable=node_observation(node,binding_graph or graph,model)
        query=queries.get(observable,{})
        valid=query.get('status') in ('sat','unsat') and not quarantined
        result[node['id']]=dict(value=int(query['status']=='sat') if valid else None,mask=valid,
            observation=observable,resolved_by_evidence=False,
            source='bounded_ir_supported_rtl_localization' if valid and node['type']=='RTL' else 'bounded_ir_semantics' if valid else 'not_labeled',
            query_hash=query.get('query_hash'),horizon=bounded_result.get('horizon'),
            replay_status=query.get('replay_status','not_run'),
            reason=None if valid else query.get('reason','no supported semantic observation'))
    return result
