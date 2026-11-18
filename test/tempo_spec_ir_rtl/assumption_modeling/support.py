"""Requirement-choice correspondence and scoped conformance; no confidence-to-fact promotion."""
import copy
from .schema import digest
from .expressions import parse, Unsupported


def _constant(value):
    ast=parse(str(value))
    if ast.op not in ('const','unsized'):raise Unsupported('not a constant')
    return ast.args[0]


def claim_applies(claim, assumption):
    kind=claim.get('choice_kind') if claim.get('kind')=='implementation_freedom' else claim.get('kind')
    if kind=='priority':
        pair=set(claim.get('operations',[])) if claim.get('kind')=='implementation_freedom' else {claim.get('higher_operation'),claim.get('lower_operation')}
        return assumption['kind']=='priority' and pair==set(assumption.get('operations',[])) and claim.get('scope','normal_operation')==assumption.get('scope','normal_operation')
    if kind in ('reset_value','boundary_policy'):
        return assumption['kind']==kind and claim.get('target')==assumption.get('target')
    return (claim.get('target')==assumption.get('target') and kind in ('state_update','hold','latency')
            and assumption['kind'] in ('state_update','hold','latency','handshake_hold','guard_condition'))


def check_behavior_claim(model, claim, budget_ms=2000, environment=()):
    """Prove a local state update implication; latency checks keep bounded scope explicit."""
    import z3
    from .transition_system import TransitionSystem
    from .witness import _constraints, _solve, _model_values
    system=TransitionSystem(model)
    target=claim['target'];width=system.widths[target]
    if target not in system.states:raise Unsupported('claim target is not a state')
    solver=z3.Solver();state=system.variables('claim_state',system.states);inputs=system.variables('claim_input',system.inputs)
    scope=dict(semantics='two_state',kind='local_implication')
    if claim['kind']=='latency':
        cycles=claim.get('cycles')
        if type(cycles) is not int or not 1<=cycles<=64:raise Unsupported('latency requires concrete 1..64 cycles')
        state,_,prefix=system.initial_pair(system)
        if prefix is not None:solver.add(*_constraints(system,system.variables('initial',system.states),prefix,environment))
        scope=dict(semantics='two_state',kind='bounded_from_initialization',horizon=cycles)
    env=system.environment(state,inputs);trigger=system.value(claim['trigger'],env)!=0
    solver.add(*_constraints(system,state,inputs,environment));solver.add(trigger)
    domain=system.domain
    if domain.reset and claim.get('scope','normal_operation')=='normal_operation':
        solver.add(inputs[domain.reset]==(1 if domain.reset_polarity in ('active_low','low') else 0))
    expected=state[target] if claim['kind']=='hold' else system.value(claim['expression'],env,width)
    from .expressions import resize
    expected=resize(expected,width)
    next_state,_=system.step(state,inputs)
    if claim['kind']=='latency':
        for step in range(1,claim['cycles']):
            step_inputs=system.variables(f'claim_input_{step}',system.inputs)
            solver.add(*_constraints(system,next_state,step_inputs,environment))
            next_state,_=system.step(next_state,step_inputs)
    feasible,reason,_,_=_solve(solver,z3.BoolVal(True),budget_ms)
    if feasible!='sat':return dict(status='unknown',reason=reason or 'vacuous trigger/environment',scope=scope)
    different=next_state[target]!=expected
    status,reason,solution,elapsed=_solve(solver,different,budget_ms)
    result=dict(status='violated' if status=='sat' else 'conformant' if status=='unsat' else 'unknown',
                tool_status=status,reason=reason,scope=scope,budget_ms=budget_ms,elapsed_ms=elapsed,
                tool_version=z3.get_version_string(),query_hash=digest(solver.sexpr()+different.sexpr()))
    if solution is not None:
        result['counterexample']=dict(state=_model_values(solution,state),inputs=_model_values(solution,inputs),
            actual=solution.eval(next_state[target],model_completion=True).as_long(),expected=solution.eval(expected,model_completion=True).as_long())
    return result


def check_choice_claim(model, assumption, claim, budget_ms=2000, environment=()):
    if assumption.get('binding_state')!='verified':return dict(status='unknown',reason='implementation binding not verified')
    kind=claim.get('kind')
    if kind=='priority':
        desired=claim['higher_operation']+'_first'
        if assumption['current_interpretation']==desired:
            return dict(status='conformant',scope=dict(kind='compiler_schedule',semantics='deterministic_lowering'))
        # A reversed syntactic order is not a behavioral violation under proven mutual exclusion
        # or equal-valued updates. Compare the complete desired update under the actual environment.
        from .alternatives import typed_patch
        from .witness import local_difference
        high=assumption['transitions'][assumption['operations'].index(claim['higher_operation'])]
        low=assumption['transitions'][assumption['operations'].index(claim['lower_operation'])]
        alternative=dict(id=assumption['id']+':source_policy',kind='behavior_change',patch_type='priority_policy',higher=high,lower=low)
        revised,_=typed_patch(model,assumption,alternative)
        evidence=local_difference(model,revised,assumption['id'],alternative['id'],budget_ms=budget_ms,environment=environment)
        return dict(status='violated' if evidence['status']=='sat' else 'conformant' if evidence['status']=='unsat' else 'unknown',
            scope=dict(kind='complete_local_update_under_environment',semantics='two_state'),difference_check=evidence)
    if kind=='reset_value':
        return dict(status='conformant' if _constant(model.state_map()[assumption['target']].reset_value)==_constant(claim['value']) else 'violated',scope=dict(kind='reset_assignment'))
    if kind=='boundary_policy':
        return dict(status='conformant' if assumption['current_interpretation']==claim['value'] else 'violated',scope=dict(kind='recognized_full_width_update'))
    if kind=='implementation_freedom':
        current=_constant(model.state_map()[assumption['target']].reset_value) if assumption['kind']=='reset_value' else assumption['current_interpretation']
        return dict(status='allowed' if current in claim.get('allowed_values',[]) else 'violated',scope=dict(kind='explicit_authorization'))
    if kind in ('state_update','hold','latency'):
        return check_behavior_claim(model,claim,budget_ms,environment)
    return dict(status='unknown',reason='unsupported requirement interpretation')


def review_assumptions(model, assumptions, requirements, *, budget_ms=2000, environment=()):
    reviewed=copy.deepcopy(assumptions);checks=[];evidence=[]
    for a in reviewed:
        relevant=[];facts=[]
        for requirement in requirements:
            for index,claim in enumerate(requirement.get('claims',[])):
                if not isinstance(claim,dict) or not claim_applies(claim,a):continue
                relevant.append(requirement['id'])
                interpretation=requirement.get('interpretation_status','proposed')
                trusted=interpretation in ('canonical_verified','reviewed')
                try:result=check_choice_claim(model,a,claim,budget_ms,environment)
                except (Unsupported,ImportError,ValueError,KeyError) as exc:result=dict(status='unknown',reason=str(exc))
                check=dict(assumption_id=a['id'],requirement_id=requirement['id'],claim=claim,
                           interpretation_status=interpretation,**result)
                checks.append(check)
                known=result['status'] in ('conformant','violated','allowed')
                if trusted and known:facts.append(check)
                event=dict(id='EV:source:'+digest([a['id'],requirement['id'],index])[:16],
                    assumption_id=a['id'],requirement_id=requirement['id'],alternative_id=None,
                    claim_kind='requirement_conformance' if trusted else 'conformance_to_proposed_interpretation',
                    claim_subject='current_choice_satisfies_source_interpretation',
                    status=result['status'],level='source_conformance',visibility='G0',
                    origin=requirement.get('origin','visible_spec'),interpretation_status=interpretation,
                    requirement_claim=claim,scope=result.get('scope'),tool_result=result,
                    resolved_tasks=['status'] if trusted and known else [])
                evidence.append(event)
        a['related_requirement_ids']=sorted(set(a.get('related_requirement_ids',[])+relevant))
        a['source_matches']=[dict(requirement_id=c['requirement_id'],verification_state='verified' if c['interpretation_status'] in ('canonical_verified','reviewed') else 'proposed') for c in checks if c['assumption_id']==a['id']]
        # Contradictory source claims are not resolved by majority vote.
        requirements_values={}
        for c in facts:
            if c['claim']['kind']!='implementation_freedom':
                identity={k:v for k,v in c['claim'].items() if k not in ('text',)}
                premise=digest([identity.get('kind'),identity.get('target'),identity.get('trigger'),identity.get('scope')])
                requirements_values.setdefault(premise,set()).add(digest(identity))
        contradictory=any(len(values)>1 for values in requirements_values.values())
        fact_statuses={c['status'] for c in facts if c['claim']['kind']!='implementation_freedom'}
        if contradictory and {'conformant','violated'} <= fact_statuses:
            a['evidence_status']='evidence_conflict'
        elif contradictory:
            a['evidence_status']='needs_source_review'
        elif any(c['status']=='violated' for c in facts):a['evidence_status']='implementation_error'
        elif any(c['status']=='allowed' for c in facts):a['evidence_status']='implementation_freedom'
        elif any(c['status']=='conformant' for c in facts):a['evidence_status']='supported'
        else:a['evidence_status']='needs_source_review'
        a['support_review']=dict(a.get('support_review',{}),matched_requirement_ids=sorted(set(relevant)),
                                 reviewed_scope='visible_spec',absence_is_proven=False)
    return reviewed,checks,evidence
