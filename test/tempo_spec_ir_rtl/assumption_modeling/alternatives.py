"""Typed, audited patches. The production IR is never changed."""
import copy
from dataclasses import asdict
from pathlib import Path
import yaml
from Compiler.rtl_compiler.lowering import build_processes, ordered_for_target
from Compiler.rtl_compiler.parser import parse_ir
from Compiler.rtl_compiler.validator import validate_ir
from .expressions import Unsupported
from .schema import digest, write_json


def typed_patch(model, assumption, alternative):
    if alternative['kind'] == 'environment_contract':
        raise Unsupported('environment protocol alternative requires independent contract evidence')
    if alternative.get('execution_state') == 'unsupported':
        raise Unsupported(alternative.get('unknown_reason', 'unsupported semantic alternative'))
    if assumption['binding_state'] != 'verified':
        raise Unsupported(assumption.get('unknown_reason') or 'binding_unknown')
    revised = copy.deepcopy(model)
    kind = alternative.get('patch_type')
    if kind == 'priority_policy':
        old_order = assumption['current_order']
        new_order = list(old_order)
        high, low = alternative['higher'], alternative['lower']
        i, j = new_order.index(high), new_order.index(low)
        if abs(i-j) != 1:
            raise Unsupported('only adjacent priority swaps preserve all other pairwise orders')
        new_order[i], new_order[j] = new_order[j], new_order[i]
        members = set(old_order)
        # Writers shared with another state would change that state's schedule too.
        target = assumption['target']
        if any(a.base_target != target for n in members for a in model.transition_map()[n].actions):
            raise Unsupported('priority patch would affect another target')
        revised.priorities = [(h, l) for h, l in revised.priorities if not (h in members and l in members)]
        revised.priorities.extend((h, l) for index, h in enumerate(new_order) for l in new_order[index+1:])
        # Keep process order consistent with the explicit total order.
        revised.processes = copy.deepcopy(build_processes(revised))
        for process in revised.processes:
            if process.name == assumption['process']:
                positions = [k for k, name in enumerate(process.transitions) if name in members]
                for position, name in zip(positions, new_order):
                    process.transitions[position] = name
                if ordered_for_target(revised, process, target) != new_order:
                    raise Unsupported('lowered alternative order does not match patch')
        edit_set = [f'IR:schedule:{target}']
    elif kind == 'reset_value':
        revised.state_map()[assumption['target']].reset_value = alternative['value']
        edit_set = [f'IR:state:{assumption["target"]}']
    elif kind in ('boundary_policy', 'assignment_expression'):
        revised.transition_map()[assumption['transition']].actions[assumption['action_index']].expression = alternative['expression']
        edit_set = list(assumption['bindings'])
    elif kind == 'guard_condition':
        transition = revised.transition_map()[assumption['transition']]
        from .expressions import parse
        known = {p.name for p in model.ports} | set(model.state_map()) | {s['name'] for s in model.derived_signals}
        if parse(alternative['guard']).symbols() - known:
            raise Unsupported('guard patch has unbound symbols')
        transition.guard = alternative['guard']
        edit_set = [f'IR:guard:{transition.name}']
    else:
        raise Unsupported('unknown typed patch')
    validate_ir(revised)
    manifest = dict(patch_type=kind, assumption_id=assumption['id'], alternative_id=alternative['id'],
                    edit_set=edit_set, semantic_edit_count=len(edit_set),
                    before_order=assumption.get('current_order'), after_order=new_order if kind == 'priority_policy' else None,
                    identity_scope='current_revision', lineage_mapping='preserved_for_audited_typed_patch',
                    old_to_new=[dict(old_id=identifier,new_id=identifier,reason='validated_local_typed_patch') for identifier in edit_set])
    manifest['patch_hash'] = digest(manifest)
    return revised, manifest


def model_document(model):
    """Canonical serialization of parsed semantics (without arbitrary LLM rewriting)."""
    doc = copy.deepcopy(model.raw)
    doc['module'] = dict(name=model.module_name, parameters=[asdict(p) for p in model.parameters], ports=[asdict(p) for p in model.ports])
    doc['states'] = [asdict(s) for s in model.states]
    doc.pop('places', None)
    doc['clock_domains'] = [asdict(d) for d in model.clock_domains]
    doc['transitions'] = []
    for t in model.transitions:
        body = asdict(t)
        body['action'] = body.pop('actions')
        body['else_action'] = body.pop('else_actions')
        doc['transitions'].append(body)
    doc['sequential_processes'] = [asdict(p) for p in model.processes]
    doc['priority'] = [dict(higher=h, lower=l) for h, l in model.priorities]
    doc.pop('priorities', None)
    return doc


def save_alternative(model, manifest, directory):
    from IRgeneration.checker1 import check_ir
    from IRgeneration.checker2 import check_ir as check_semantics
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'ir.yaml'
    path.write_text(yaml.safe_dump(model_document(model), sort_keys=False, allow_unicode=True), encoding='utf-8')
    before = parse_ir(path)
    check_ir(path, directory / 'checker1.json')
    check_semantics(path, directory / 'checker2.json')
    after = parse_ir(path)
    # Checker normalizations may not silently change the semantic comparison.
    def semantic(m):
        return dict(states=[asdict(s) for s in m.states], transitions=[asdict(t) for t in m.transitions],
                    domains=[asdict(d) for d in m.clock_domains],
                    orders={f'{p.name}:{s.name}': ordered_for_target(m, p, s.name) for p in build_processes(m) for s in m.states})
    if semantic(before) != semantic(after):
        raise Unsupported('checker normalization changed alternative semantics; comparison quarantined')
    write_json(directory / 'patch_manifest.json', manifest)
    return path, after
