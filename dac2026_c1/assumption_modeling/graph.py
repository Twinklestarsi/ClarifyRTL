"""Revision-scoped heterogeneous graph; hardware feedback cycles are allowed."""
import copy
from Compiler.rtl_compiler.backend import _derived_expression
from Compiler.rtl_compiler.lowering import build_processes, ordered_for_target
from .expressions import parse, Unsupported
from .schema import digest, ir_id


def build_graph(model, requirements, assumptions, mapping, metadata):
    graph = dict(metadata, nodes=[], edges=[], coverage=dict(dependency_complete=True, unsupported_expressions=[]))
    nodes = {}
    def node(identifier, kind, text, subtype=None, **attributes):
        if identifier not in nodes:
            item = dict(id=identifier, entity_id=identifier,
                        revision_id=digest([metadata.get('spec_hash'),metadata.get('ir_hash'),metadata.get('rtl_hash'),identifier,text,subtype,attributes]),
                        identity_scope='current_revision', type=kind, subtype=subtype,
                        text=str(text), attributes=attributes, provenance_ids=[])
            nodes[identifier] = item
            graph['nodes'].append(item)
        else:
            existing = nodes[identifier]['attributes']
            for key, value in attributes.items():
                existing[key] = sorted(set(existing.get(key, []) + value)) if key == 'roles' else value
        return identifier
    def edge(src, dst, relation, subtype=None, origin='deterministic_structure', verification_state='verified', **extra):
        graph['edges'].append(dict(source=src, target=dst, relation=relation, subtype=subtype,
                                   origin=origin, verification_state=verification_state,
                                   evidence_ids=[], snapshot_visibility='G0', **extra))
    symbols = {}
    for port in model.ports:
        kind = 'state' if port.name in model.state_map() else 'port'
        identifier = ir_id(kind, port.name)
        symbols[port.name] = node(identifier, 'IR', port.name, kind, width=port.width,
                                  roles=['state', port.direction] if kind == 'state' else [port.direction],signed=port.signed,datatype=port.datatype)
    for state in model.states:
        symbols[state.name] = node(ir_id('state', state.name), 'IR', state.name, 'state', width=state.width,
                                  roles=['state'], clock_domain=state.clock_domain, reset_value=state.reset_value,datatype=state.datatype,
                                  signed=('signed' in state.datatype and 'unsigned' not in state.datatype) or getattr(model.port_map().get(state.name),'signed',False))
    for signal in model.derived_signals:
        identifier = ir_id('derived', signal['name'])
        if signal['name'] in symbols:
            edge(symbols[signal['name']], identifier, 'depends-on', 'alias')
        symbols[signal['name']] = node(identifier, 'IR', _derived_expression(signal), 'derived', width=signal.get('width'))
    parameter_names = {parameter.name for parameter in model.parameters}

    def reads(identifier, expression):
        try:
            ast = parse(expression)
            for name in sorted(ast.symbols()):
                if name in symbols:
                    edge(identifier, symbols[name], 'reads')
                elif name in parameter_names:
                    # Compile-time parameters are constants, not graph nodes.
                    continue
                else:
                    raise Unsupported(f'unbound symbol {name}')
        except Unsupported as exc:
            graph['coverage']['dependency_complete'] = False
            graph['coverage']['unsupported_expressions'].append(dict(element_id=identifier, reason=str(exc)))
    for signal in model.derived_signals:
        reads(ir_id('derived', signal['name']), _derived_expression(signal))
    for domain in model.clock_domains:
        clock = node(ir_id('clock', domain.name), 'IR', domain.clock, 'clock', edge=domain.edge)
        if domain.clock in symbols:
            edge(clock, symbols[domain.clock], 'depends-on', 'clock_signal')
        if domain.reset:
            reset = node(ir_id('reset', domain.name), 'IR', domain.reset, 'reset', polarity=domain.reset_polarity, reset_type=domain.reset_type)
            if domain.reset in symbols:
                edge(reset, symbols[domain.reset], 'reads')
            for state in model.states:
                if state.clock_domain == domain.name:
                    edge(ir_id('state', state.name), reset, 'depends-on', 'reset')
    for transition in model.transitions:
        tid = node(ir_id('transition', transition.name), 'IR', transition.name, 'transition')
        gid = node(ir_id('guard', transition.name), 'IR', transition.guard, 'guard')
        edge(tid, gid, 'depends-on', 'control')
        reads(gid, transition.guard)
        for kind, actions in (('action', transition.actions), ('else_action', transition.else_actions)):
            for index, action in enumerate(actions):
                aid = node(ir_id(kind, transition.name, index), 'IR', f'{action.target} = {action.expression}', kind)
                edge(tid, aid, 'contains')
                edge(aid, gid, 'depends-on', 'control')
                reads(aid, action.expression)
                if action.base_target in symbols:
                    edge(aid, symbols[action.base_target], 'writes', 'whole_scalar' if action.target == action.base_target else 'partial_unknown')
    for process in build_processes(model):
        for state in model.states:
            names = ordered_for_target(model, process, state.name)
            if not names:
                continue
            sid = node(ir_id('schedule', state.name), 'IR', ' > '.join(names), 'schedule', order=names)
            edge(ir_id('state', state.name), sid, 'depends-on', 'state_update')
            for name in names:
                edge(sid, ir_id('transition', name), 'depends-on', 'scheduling')
    for req in requirements:
        node(req['id'], 'Requirement', req['text'], source=req['source'],
             **{key:copy.deepcopy(req.get(key)) for key in ('trigger','expected_behavior','temporal_relation','related_signals','explicit_freedom','scope','claims','interpretation_status','origin')})
        for name in req.get('related_signals', []):
            if name in symbols:
                edge(req['id'], symbols[name], 'relates-to', 'requirement_signal', origin='grounded_symbol_reference')
    for assumption in assumptions:
        aid = node(assumption['id'], 'Assumption', assumption['current_interpretation'], assumption['kind'],
                   binding_state=assumption['binding_state'], alternative_ids=[b['id'] for b in assumption['alternatives']],
                   scope=assumption.get('scope'), implemented=True, origin=assumption.get('proposal_origin'),
                   alternatives=[{k:b[k] for k in ('id','value','kind') if k in b} for b in assumption['alternatives']])
        for binding in assumption['bindings']:
            if binding in nodes:
                edge(aid, binding, 'binds-to')
        for rid in assumption['related_requirement_ids']:
            edge(aid, rid, 'relates-to', verification_state='proposed', origin='source_retrieval')
        for match in assumption.get('source_matches', []):
            if match['requirement_id'] in nodes:
                edge(aid, match['requirement_id'], 'specified-by', origin='grounded_requirement_interpretation',
                     verification_state=match['verification_state'], claim_kind='requirement_choice')
    for region in mapping.get('regions', []):
        rid = node(region['region_id'], 'RTL', region.get('code', region['role']), region['role'],
                   line_start=region['line_start'], line_end=region['line_end'], precision=region['precision'])
        for identifier in region['ir_ids']:
            if identifier in nodes:
                edge(identifier, rid, 'implemented-by', origin='compiler_emission')
    validate_graph(graph)
    return graph


def with_evidence(graph, evidence, snapshot_id):
    result = copy.deepcopy(graph)
    result['snapshot_id'] = snapshot_id
    known = {node['id'] for node in result['nodes']}
    for index, item in enumerate(evidence):
        identifier = item.get('id') or f'EV:{snapshot_id}:{index}'
        if identifier in known:
            identifier += ':' + digest(item)[:10]
        known.add(identifier)
        attrs = copy.deepcopy(item)
        attrs.setdefault('origin', 'semantic_tool')
        attrs.setdefault('claim_subject', 'alternatives_differ' if item['claim_kind'] == 'local_next_state_difference' else item['claim_kind'])
        result['nodes'].append(dict(id=identifier, type='Evidence', subtype=item['claim_kind'],
            text=f"{item['claim_kind']}: {item['status']}", attributes=attrs, provenance_ids=[]))
        positive = item['status'] in ('sat','conformant','allowed','confirmed')
        negative = item['status'] in ('unsat','violated','rejected')
        relation = 'supports' if positive else 'contradicts' if negative else 'relates-to'
        result['edges'].append(dict(source=identifier, target=item['assumption_id'], relation=relation,
            origin=attrs['origin'], verification_state='verified' if positive or negative else 'unknown',
            evidence_ids=[identifier], snapshot_visibility=item.get('visibility', 'G2'),
            claim_kind=item['claim_kind'], claim_subject=attrs['claim_subject'], alternative_id=item.get('alternative_id')))
        if item.get('requirement_id') in known:
            result['edges'].append(dict(source=identifier,target=item['requirement_id'],relation='depends-on',
                subtype='source_interpretation',origin=attrs['origin'],verification_state=item.get('interpretation_status','proposed'),
                evidence_ids=[identifier],snapshot_visibility=item.get('visibility','G2')))
    validate_graph(result)
    return result


def validate_graph(graph):
    identifiers = [n['id'] for n in graph['nodes']]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('duplicate graph ID')
    known = set(identifiers)
    if any(e['source'] not in known or e['target'] not in known for e in graph['edges']):
        raise ValueError('dangling graph reference')
