"""Explicit G0/G1 input protocol. Raw visible evidence is distinct from supervision."""
import copy
from .graph import validate_graph
from .schema import SNAPSHOTS, digest, write_json

_ALLOWED_NODE_ATTRIBUTES = {
    'Requirement': ('source','trigger','expected_behavior','temporal_relation','related_signals','explicit_freedom','scope','claims','interpretation_status','origin'),
    'Assumption': ('binding_state','alternative_ids','alternatives','scope','implemented','origin'),
    'IR': ('width','roles','clock_domain','reset_value','edge','polarity','reset_type','order','datatype','signed'),
    'RTL': ('line_start','line_end','precision'),
    'Property': ('origin','source_requirement','availability'),
    'Evidence': ('status','claim_kind','claim_subject','level','alternative_id','assumption_id','requirement_id','scope','budget_ms','origin','interpretation_status','requirement_claim','source')}
_ALLOWED_RELATIONS = {'specified-by','implemented-by','reads','writes','depends-on','supports','contradicts','checked-by','relates-to','binds-to','contains'}


def export_graph(graph, representations=('Spec','IR','RTL'), *, include_local_witness=True):
    if graph.get('snapshot_id') not in SNAPSHOTS[:2]:raise ValueError('Only frozen G0/G1 can be exported as model inputs')
    if set(representations)-{'Spec','IR','RTL'}:raise ValueError('invalid representation ablation')
    result={k:copy.deepcopy(graph.get(k)) for k in ('schema_version','snapshot_id','spec_hash','ir_hash','rtl_hash','tool_config')}
    # Hashes of removed views cannot serve as hidden surrogate features.
    for view,key in (('Spec','spec_hash'),('IR','ir_hash'),('RTL','rtl_hash')):
        if view not in representations:result[key]=None
    result['nodes']=[];result['edges']=[]
    for node in graph['nodes']:
        kind=node['type'];attrs=node.get('attributes',{})
        if kind not in _ALLOWED_NODE_ATTRIBUTES:raise ValueError('Unsupported input node type')
        if kind=='Requirement' and 'Spec' not in representations:continue
        if kind=='IR' and 'IR' not in representations:continue
        if kind=='RTL' and 'RTL' not in representations:continue
        if kind=='Property':
            if attrs.get('availability')!='G0' or 'IR' not in representations:continue
            if attrs.get('origin')=='spec_derived' and 'Spec' not in representations:continue
        if kind=='Evidence':
            level=attrs.get('level');visibility=attrs.get('visibility')
            if level=='local':
                if not include_local_witness or graph['snapshot_id']!='post_local_witness' or visibility!='G1':continue
            elif visibility!='G0' or level not in ('user_answer','authoritative_document'):continue
            if 'Spec' not in representations and level!='local':continue
            if 'IR' not in representations and level in ('source_conformance','local'):continue
            # Standardized summaries; no traces, arbitrary logs, costs, oracle paths, or final labels.
            text=f"{attrs.get('claim_kind','source_evidence')} {attrs.get('status','unknown')}"
        else:text=node['text']
        filtered={key:copy.deepcopy(attrs[key]) for key in _ALLOWED_NODE_ATTRIBUTES[kind] if key in attrs}
        if kind=='Requirement':
            # Source paths are represented by document identity + ranges, never filesystem names.
            filtered['source']={k:v for k,v in filtered.get('source',{}).items() if k in ('document_id','line_start','line_end','char_start','char_end')}
        result['nodes'].append(dict(id=node['id'],type=kind,subtype=node.get('subtype'),text=text,attributes=filtered))
    known={n['id'] for n in result['nodes']}
    for edge in graph['edges']:
        if edge['relation'] in _ALLOWED_RELATIONS and edge['source'] in known and edge['target'] in known and edge.get('snapshot_visibility','G0') in ('G0','G1'):
            result['edges'].append({k:copy.deepcopy(edge.get(k)) for k in ('source','target','relation','subtype','origin','verification_state','claim_kind','claim_subject','alternative_id')})
    result['feature_whitelist_version']='c1-input-2'
    result['representations']=list(representations);result['include_local_witness']=include_local_witness
    result['input_hash']=digest(result)
    validate_graph(result)
    return result


def export_case(state_directory, output_directory, *, snapshot='G1', representations=('Spec','IR','RTL'), include_local_witness=True):
    import json
    from pathlib import Path
    source,output=Path(state_directory),Path(output_directory)
    if snapshot not in ('G0','G1'):raise ValueError('snapshot must be G0 or G1')
    graph=export_graph(json.loads((source/f'graph_{snapshot}.json').read_text()),representations,include_local_witness=include_local_witness)
    write_json(output/'input'/'graph.json',graph)
    choices=json.loads((source/'assumptions.json').read_text())['assumptions']
    queries=[dict(id=a['id'],current_interpretation=a['current_interpretation'],kind=a['kind'],
        alternatives=[{k:b[k] for k in ('id','kind','value') if k in b} for b in a['alternatives']]) for a in choices]
    write_json(output/'input'/'queries.json',queries)
    write_json(output/'input'/'visibility_policy.json',dict(snapshot=graph['snapshot_id'],whitelist='c1-input-2',
        representations=list(representations),include_local_witness=include_local_witness,
        forbidden=['oracle','gold_status','future_answers','bounded_results','repair_cost','full_alternative_ir']))
    return graph
