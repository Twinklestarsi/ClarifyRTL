"""Import actual properties at their available snapshot, preserving origin and binding."""
import copy
import json
from pathlib import Path
import yaml
from .graph import validate_graph
from .schema import digest


def attach_properties(graph,document,visibility='G0',namespace='available'):
    result=copy.deepcopy(graph);known={n['id'] for n in result['nodes']}
    for index,prop in enumerate(document.get('sva_properties',[]) if isinstance(document,dict) else []):
        identifier=f'PROPERTY:{namespace}:{index}:{prop.get("name","unnamed")}'
        if identifier in known:continue
        known.add(identifier)
        origin='spec_derived' if prop.get('generation_source')=='spec' else 'diagnostic_monitor' if prop.get('role')=='diagnostic_monitor' else 'ir_derived'
        result['nodes'].append(dict(id=identifier,type='Property',subtype=prop.get('category'),text=json.dumps(prop,ensure_ascii=False),
            attributes=dict(origin=origin,source_requirement=prop.get('source_requirement'),availability=visibility),provenance_ids=[]))
        scope=prop.get('scope') or {}
        for kind,names in (('transition',scope.get('related_transitions',[])),('state',scope.get('related_states',[])),('port',scope.get('related_signals',[]))):
            for name in names:
                target=f'IR:{kind}:{name}'
                if target in known:result['edges'].append(dict(source=target,target=identifier,relation='checked-by',
                    origin='actual_property_scope',verification_state='bound',evidence_ids=[],snapshot_visibility=visibility))
        requirement=(prop.get('source_requirement') or {}).get('id')
        if requirement in known:result['edges'].append(dict(source=identifier,target=requirement,relation='relates-to',
            origin='property_source_reference',verification_state='proposed',evidence_ids=[],snapshot_visibility=visibility))
    validate_graph(result)
    return result


def load_available_properties(ir_path,path,directory):
    import shutil
    from sva_generation.property_ir import check_property_ir
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    copy_path=directory/'available_property_ir.yaml';shutil.copyfile(path,copy_path)
    check_property_ir(ir_path,copy_path,directory/'available_property_checker.json')
    return yaml.safe_load(copy_path.read_text(encoding='utf-8'))
