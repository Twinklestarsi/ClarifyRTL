"""Bind frozen proposals to current compiler entities before executing any alternative."""
import copy
from .schema import digest, ir_id
from .expressions import parse, Unsupported

PATCH_TYPES = {'priority_policy','reset_value','boundary_policy','assignment_expression','guard_condition'}


def inventory(model):
    return dict(module=model.module_name,
        states=[dict(name=s.name,width=s.width,reset_value=s.reset_value,clock_domain=s.clock_domain) for s in model.states],
        transitions=[dict(name=t.name,kind=t.transition_type,guard=t.guard,clock_domain=t.clock_domain,
                         actions=[dict(target=a.target,expression=a.expression) for a in t.actions]) for t in model.transitions],
        signals=[p.name for p in model.ports]+[s.name for s in model.states]+[s['name'] for s in model.derived_signals],
        priorities=[list(pair) for pair in model.priorities])


def merge_requirements(current, proposed):
    merged=copy.deepcopy(current);aliases={};by_id={r['id']:r for r in merged}
    for item in proposed:
        existing=next((r for r in merged if r['text']==item['text'] and r['source']['line_start']==item['source']['line_start'] and r['source']['line_end']==item['source']['line_end']),None)
        if existing:
            aliases[item['id']]=existing['id']
            if existing.get('interpretation_status')=='unreviewed':
                identifier=existing['id'];existing.update(copy.deepcopy(item));existing['id']=identifier
            else:
                existing['semantic_proposal']={k:copy.deepcopy(item.get(k)) for k in ('trigger','expected_behavior','temporal_relation','claims')}
        else:
            fresh=copy.deepcopy(item)
            if fresh['id'] in by_id:fresh['id']='REQ_LLM_'+digest(item)[:12]
            aliases[item['id']]=fresh['id'];merged.append(fresh);by_id[fresh['id']]=fresh
    return merged,aliases


def bind_proposals(model, rules, proposals, requirements, aliases=None, origin='frozen_llm'):
    """Rejected proposals remain in diagnostics; they cannot mutate production IR."""
    assumptions=copy.deepcopy(rules);validation=[];aliases=aliases or {}
    known={r['id'] for r in requirements};states=model.state_map();transitions=model.transition_map()
    signals=set(inventory(model)['signals'])
    for proposal in proposals:
        pid=proposal.get('id','unnamed') if isinstance(proposal,dict) else 'invalid'
        try:
            if not isinstance(proposal,dict):raise ValueError('candidate must be an object')
            reqs=[aliases.get(r,r) for r in proposal.get('requirement_ids',[])]
            if set(reqs)-known:raise ValueError('candidate cites an unknown requirement')
            target=proposal.get('target');transition_name=proposal.get('transition');index=proposal.get('action_index',0)
            kind=proposal.get('kind','semantic_choice')
            matched=None
            if kind=='priority':
                pair=set(proposal.get('transitions',[]))
                if not pair:
                    priority_patch=next((b.get('patch',{}) for b in proposal.get('alternatives',[]) if isinstance(b,dict) and b.get('patch',{}).get('type')=='priority_policy'),{})
                    pair={priority_patch.get('higher'),priority_patch.get('lower')}
                matched=next((a for a in assumptions if a['kind']=='priority' and a['target']==target and set(a['transitions'])==pair),None)
                if not matched:raise ValueError('priority pair is not a verified current writer pair')
                candidate=copy.deepcopy(matched)
            elif kind in ('reset_value','boundary_policy'):
                matches=[a for a in assumptions if a['kind']==kind and a['target']==target]
                if kind=='boundary_policy' and transition_name:
                    matches=[a for a in matches if a.get('transition')==transition_name and a.get('action_index')==index]
                if len(matches)!=1:raise ValueError('reset/boundary implementation binding is missing or ambiguous')
                matched=matches[0]
                candidate=copy.deepcopy(matched)
            else:
                if target not in states:raise ValueError('candidate target is not a current scalar state')
                if transition_name not in transitions:raise ValueError('candidate transition is not in current IR')
                transition=transitions[transition_name]
                if type(index) is not int or not 0<=index<len(transition.actions):raise ValueError('invalid action binding')
                action=transition.actions[index]
                if action.target!=target:raise ValueError('candidate does not bind a full current target')
                if transition.transition_type!='sequential':raise ValueError('first implementation supports sequential alternative bindings')
                candidate=dict(id=('A:llm:' if origin=='frozen_llm' else 'A:rule:')+digest([kind,target,transition_name,index])[:16],kind=kind,target=target,
                    transition=transition_name,action_index=index,current_interpretation=action.expression,
                    current_guard=transition.guard,bindings=[ir_id('guard',transition_name),ir_id('action',transition_name,index)],
                    binding_state='verified',scope=proposal.get('scope','normal_operation'),
                    support_review=dict(state='needs_source_review'),related_requirement_ids=[],alternatives=[])
            candidate['related_requirement_ids']=sorted(set(candidate.get('related_requirement_ids',[])+reqs))
            candidate['llm_description']=proposal.get('current_interpretation','')
            candidate['proposal_origin']=('rule_plus_frozen_llm' if matched else 'frozen_llm_bound_to_ir') if origin=='frozen_llm' else 'deterministic_visible_ir'
            candidate['domain_closed']=False;candidate['refinement_exit']='requires_refinement'
            alternatives=list(candidate['alternatives'])
            for proposed_alt in proposal.get('alternatives',[]):
                if not isinstance(proposed_alt,dict) or not isinstance(proposed_alt.get('value'),str):raise ValueError('invalid alternative description')
                alt=dict(kind=proposed_alt.get('kind','behavior_change'),value=proposed_alt['value'])
                if alt['kind']=='environment_contract':
                    alt['execution_state']='requires_environment_evidence'
                else:
                    patch=proposed_alt.get('patch') or {}
                    patch_type=patch.get('type')
                    if patch_type not in PATCH_TYPES:
                        alt.update(execution_state='unsupported',unknown_reason='no supported typed patch')
                    else:
                        alt['patch_type']=patch_type
                        if patch_type=='priority_policy':
                            if not matched or matched['kind']!='priority':raise ValueError('priority patch lacks verified schedule')
                            if {patch.get('higher'),patch.get('lower')}!=set(candidate['transitions']):raise ValueError('priority patch references unrelated operations')
                            alt.update(higher=patch['higher'],lower=patch['lower'])
                        elif patch_type=='reset_value':
                            if candidate['kind']!='reset_value':raise ValueError('reset patch lacks reset binding')
                            value=str(patch.get('value',''));ast=parse(value)
                            if ast.op not in ('const','unsized'):raise ValueError('reset alternative must be a constant')
                            width=int(states[target].width)
                            if not 0<=ast.args[0]<(1<<width):raise ValueError('reset alternative does not fit target')
                            alt['value']=f"{width}'d{ast.args[0]}"
                        else:
                            field='guard' if patch_type=='guard_condition' else 'expression'
                            value=patch.get(field)
                            if not isinstance(value,str):raise ValueError('missing typed expression')
                            if parse(value).symbols()-signals:raise ValueError('patch expression references unknown symbols')
                            if not transition_name and not candidate.get('transition'):raise ValueError('patch has no transition binding')
                            alt[field]=value
                alt['id']=candidate['id']+':B:'+digest(alt)[:12]
                if not any(a.get('patch_type')==alt.get('patch_type') and a.get('value')==alt['value'] for a in alternatives):alternatives.append(alt)
            candidate['alternatives']=alternatives
            if matched:assumptions[assumptions.index(matched)]=candidate
            else:assumptions.append(candidate)
            validation.append(dict(proposal_id=pid,status='bound',assumption_id=candidate['id'],requirement_ids=reqs))
        except (ValueError,TypeError,KeyError,Unsupported) as exc:
            validation.append(dict(proposal_id=pid,status='rejected',reason=str(exc)))
    return assumptions,validation
