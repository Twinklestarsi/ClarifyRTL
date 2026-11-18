"""Scan all supported implementation choices, including explicitly supported ones."""
import re
from Compiler.rtl_compiler.lowering import build_processes, ordered_for_target
from .expressions import parse, Unsupported
from .requirements import review_priority
from .schema import ir_id


def operation(transition):
    return transition.guard if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', transition.guard) else transition.name.removesuffix('_op')


def scan_choices(model, requirements):
    choices = []
    transitions = model.transition_map()
    for process in build_processes(model):
        for state in model.states:
            names = ordered_for_target(model, process, state.name)
            for high, low in zip(names, names[1:]):
                first, second = transitions[high], transitions[low]
                op_high, op_low = operation(first), operation(second)
                claims, freedoms = review_priority(requirements, op_high, op_low)
                valid = (state.type not in ('memory', 'ram') and
                         all(a.target == state.name for t in (first, second) for a in t.actions if a.base_target == state.name) and
                         all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', t.guard) for t in (first, second)) and
                         not any(t.else_actions for t in (first, second)))
                identifier = f'A:priority:{state.name}:{high}:{low}'
                choices.append(dict(id=identifier, kind='priority', target=state.name,
                    current_interpretation=f'{op_high}_first', operations=[op_high, op_low],
                    transitions=[high, low], process=process.name, current_order=names,
                    scope='normal_operation', binding_state='verified' if valid else 'binding_unknown',
                    unknown_reason=None if valid else 'compound/masked guards or partial/else updates need operation semantics',
                    origin='explicit_priority' if (high, low) in model.priorities else 'process_order_fallback',
                    bindings=[ir_id('schedule', state.name), ir_id('transition', high), ir_id('transition', low)],
                    related_requirement_ids=[r['id'] for r in requirements if any(op in r.get('related_signals', []) for op in (op_high, op_low))],
                    support_review=dict(state='proposed_source_match' if claims else 'explicit_freedom_match' if freedoms else 'not_found_in_reviewed_scope', claims=claims, freedom_sources=freedoms),
                    alternatives=[dict(id=identifier + ':reverse', kind='behavior_change', patch_type='priority_policy',
                                       value=f'{op_low}_first', higher=low, lower=high),
                                  dict(id=identifier + ':exclusive', kind='environment_contract', value='exclusive_inputs')],
                    domain_closed=False, refinement_exit='requires_refinement', proposal_origin='rule'))
    for state in model.states:
        domain = model.domain_map().get(state.clock_domain)
        if domain and domain.reset:
            identifier = f'A:reset:{state.name}'
            try:
                ast = parse(state.reset_value)
                value = ast.args[0] if ast.op in ('const', 'unsized') else None
                width = int(state.width)
            except (ValueError, Unsupported):
                value, width = None, 0
            choices.append(dict(id=identifier, kind='reset_value', target=state.name,
                current_interpretation=state.reset_value, domain_closed=False, refinement_exit='requires_refinement',
                binding_state='verified' if value is not None and width > 0 and state.type not in ('memory','ram') and any(t.transition_type=='sequential' and any(a.base_target==state.name for a in t.actions) for t in model.transitions) else 'binding_unknown',
                bindings=[ir_id('state', state.name), ir_id('reset', state.clock_domain)],
                related_requirement_ids=[], support_review=dict(state='needs_source_review'),
                alternatives=[dict(id=identifier+':alternative', kind='behavior_change', patch_type='reset_value',
                                   value=f"{width}'d{1 if value == 0 else 0}")] if value is not None and width > 0 else [],
                proposal_origin='rule'))
    for t in model.transitions:
        if t.transition_type != 'sequential':
            continue
        for index, action in enumerate(t.actions):
            state = model.state_map().get(action.target)
            if not state:
                continue
            try:
                ast = parse(action.expression)
            except Unsupported:
                continue
            # Recognize only exactly typed increment and max-saturation policies.
            def increment(node):
                return (node.op == '+' and node.args[0].op == 'var' and node.args[0].args[0] == state.name
                        and node.args[1].op == 'const' and node.args[1].args[0] == 1
                        and str(node.args[1].args[1]) == str(state.width))
            policy, update = None, None
            if increment(ast):
                policy = 'wrap'
            elif ast.op == '?:':
                condition, yes, no = ast.args
                if (condition.op == '==' and condition.args[0].op == 'var' and condition.args[0].args[0] == state.name
                        and condition.args[1].op == 'const' and str(condition.args[1].args[1]) == str(state.width)
                        and condition.args[1].args[0] == (1 << int(state.width))-1
                        and yes.op == 'var' and yes.args[0] == state.name and increment(no)):
                    policy = 'saturate'
            if policy:
                width = int(state.width)
                maximum = (1 << width)-1
                expression = f"{state.name} + {width}'d1"
                if policy == 'wrap':
                    expression = f"({state.name} == {width}'d{maximum}) ? {state.name} : ({expression})"
                identifier = f'A:boundary:{t.name}:{index}'
                choices.append(dict(id=identifier, kind='boundary_policy', target=state.name,
                    current_interpretation=policy, binding_state='verified', domain_closed=False,
                    refinement_exit='requires_refinement', transition=t.name, action_index=index,
                    bindings=[ir_id('action', t.name, index)], related_requirement_ids=[],
                    support_review=dict(state='needs_source_review'), proposal_origin='rule',
                    alternatives=[dict(id=identifier+':alternative', kind='behavior_change', patch_type='boundary_policy',
                        value='saturate' if policy=='wrap' else 'wrap', expression=expression)]))
    return choices


def scan_requirement_choices(model, requirements, choices):
    """Bind explicit update/hold/latency clauses to a unique writer; no guessed binding."""
    result=list(choices)
    for requirement in requirements:
        for claim in requirement.get('claims',[]):
            if not isinstance(claim,dict) or claim.get('kind') not in ('state_update','hold','latency'):continue
            target=claim.get('target')
            try:trigger_symbols=parse(claim['trigger']).symbols()
            except (Unsupported,KeyError):continue
            writers=[]
            for transition in model.transitions:
                if transition.transition_type!='sequential':continue
                try:guard_symbols=parse(transition.guard).symbols()
                except Unsupported:continue
                if trigger_symbols and not trigger_symbols & guard_symbols:continue
                for index,action in enumerate(transition.actions):
                    if action.target==target:writers.append((transition,index,action))
            if len(writers)!=1:continue
            transition,index,action=writers[0]
            identifier=f'A:behavior:{claim["kind"]}:{transition.name}:{index}'
            if any(a['id']==identifier for a in result):continue
            alternative=[]
            expression=target if claim['kind']=='hold' else claim.get('expression')
            if expression is not None and claim['kind']!='latency' and expression!=action.expression:
                alternative=[dict(id=identifier+':requirement',kind='behavior_change',patch_type='assignment_expression',
                                  value='source_behavior',expression=expression)]
            result.append(dict(id=identifier,kind=claim['kind'],target=target,transition=transition.name,action_index=index,
                current_interpretation=action.expression,current_guard=transition.guard,binding_state='verified',
                bindings=[ir_id('guard',transition.name),ir_id('action',transition.name,index)],
                related_requirement_ids=[requirement['id']],support_review=dict(state='needs_source_review'),
                alternatives=alternative,domain_closed=False,refinement_exit='requires_refinement',
                proposal_origin='source_clause_and_verified_writer',scope=claim.get('scope','normal_operation')))
    return result
