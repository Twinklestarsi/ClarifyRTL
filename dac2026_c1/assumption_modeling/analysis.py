"""Contribution 1: grounded choices, heterogeneous evidence graphs and scoped predictions."""
import json
import re
import shutil
from pathlib import Path
from Compiler.rtl_compiler.parser import parse_ir
from Compiler.rtl_compiler.errors import CompilerError
from .alternatives import typed_patch, save_alternative
from .binding import inventory, merge_requirements, bind_proposals
from .candidates import scan_choices, scan_requirement_choices
from .conformance import check_conformance
from .evidence import load_evidence
from .expressions import Unsupported
from .graph import build_graph, with_evidence
from .impact import structural_cone
from .predict import empty_predictions
from .requirements import extract_requirements, frozen_llm_proposals, load_reviewed_requirements
from .report import write_report
from .schema import envelope, write_json, digest
from .support import review_assumptions
from .witness import local_difference, bounded_impact, unknown


def _output_protocol(model, requirements):
    outputs=[];sources=[]
    for port in model.ports:
        if port.direction!='output':continue
        pattern=rf'\b{re.escape(port.name)}\s+is\s+an?\s+always\s+valid\s+output\b|{re.escape(port.name)}\s*始终是有效输出'
        for req in requirements:
            if re.search(pattern,req['text'],re.I):outputs.append(port.name);sources.append(req['id'])
    return dict(always_valid=sorted(set(outputs)),evidence_ids=sorted(set(sources))) if sources else None


def start_analysis(*, ir_path, rtl_path, source_map_path, spec_path, output_dir,
                   attempt_id=0, case_id='run', contract=None, horizon=16, budget_ms=2000,
                   llm_options=None, checkpoint=None, encoder_path=None, output_protocol=None,
                   requirements_path=None, evidence_path=None, max_alternatives=2,
                   calibrator_path=None, revision_cost_scope=None, property_path=None,
                   candidate_proposals=None, executable_assumption_ids=None):
    if horizon<1 or budget_ms<1 or max_alternatives<1:raise ValueError('analysis budgets must be positive')
    directory=Path(output_dir);directory.mkdir(parents=True,exist_ok=True)
    meta=envelope(spec_path,ir_path,rtl_path,case_id,attempt_id,tool_config=dict(
        horizon=horizon,local_budget_ms=budget_ms,max_executable_alternatives=max_alternatives,
        semantics='two_state',revision_cost_scope=revision_cost_scope))
    shutil.copyfile(ir_path,directory/'normalized_ir.yaml')
    model=parse_ir(ir_path)
    text=Path(spec_path).read_text(encoding='utf-8') if spec_path else ''
    requirements=load_reviewed_requirements(requirements_path,text,meta['spec_hash']) if requirements_path else extract_requirements(text)
    assumptions=scan_requirement_choices(model,requirements,scan_choices(model,requirements))
    proposal_validation=[]
    extraction=dict(provider='reviewed' if requirements_path else 'canonical_rules',
                    semantic_requirements=sum(r['expected_behavior'] is not None for r in requirements),
                    full_semantic_coverage=False,llm_frozen=True)
    if llm_options:
        proposals=frozen_llm_proposals(text,inventory(model),signals=inventory(model)['signals'],**llm_options)
        write_json(directory/'llm_proposals.json',dict(meta,**proposals))
        requirements,aliases=merge_requirements(requirements,proposals['proposals']['requirements'])
        assumptions=scan_requirement_choices(model,requirements,scan_choices(model,requirements))
        assumptions,proposal_validation=bind_proposals(model,assumptions,proposals['proposals']['candidates'],requirements,aliases)
        extraction.update(provider='frozen_llm_with_binding_checks',model=proposals['model'],
                          semantic_requirements=sum(r['expected_behavior'] is not None for r in requirements))
    if candidate_proposals is not None:
        assumptions, extra_validation = bind_proposals(model, assumptions, candidate_proposals, requirements, {}, origin='deterministic_visible_ir')
        proposal_validation.extend(extra_validation)
        extraction['candidate_provider'] = 'deterministic_visible_ir_proposals'
    environment=(contract or {}).get('environment_constraints',[])
    assumptions,requirement_checks,source_evidence=review_assumptions(model,assumptions,requirements,budget_ms=budget_ms,environment=environment)
    imported=load_evidence(evidence_path,meta,{a['id'] for a in assumptions},{r['id'] for r in requirements})
    from .support import check_choice_claim
    for event in imported:
        if event['status']!='confirmed' or not isinstance(event.get('requirement_claim'),dict):continue
        assumption=next(a for a in assumptions if a['id']==event['assumption_id'])
        try:proof=check_choice_claim(model,assumption,event['requirement_claim'],budget_ms,environment)
        except (Unsupported,ImportError,ValueError,KeyError) as exc:proof=dict(status='unknown',reason=str(exc))
        if proof['status'] in ('conformant','violated','allowed'):
            status='implementation_error' if proof['status']=='violated' else 'implementation_freedom' if proof['status']=='allowed' else 'supported'
            previous=assumption['evidence_status']
            assumption['evidence_status']=status if previous in ('needs_source_review',status) else 'evidence_conflict'
        source_evidence.append(dict(event,id=event['id']+':conformance',level='source_conformance',
            claim_kind='requirement_conformance',claim_subject='current_choice_satisfies_source_interpretation',
            status=proof['status'],tool_result=proof,scope=proof.get('scope'),interpretation_status='reviewed'))
    source_evidence.extend(imported)
    protocol=output_protocol if output_protocol is not None else _output_protocol(model,requirements)
    write_json(directory/'requirements.json',dict(meta,requirements=requirements))
    write_json(directory/'assumptions.json',dict(meta,assumptions=assumptions))
    write_json(directory/'extraction_manifest.json',dict(meta,**extraction))
    write_json(directory/'proposal_validation.json',dict(meta,results=proposal_validation))
    write_json(directory/'requirement_checks.json',dict(meta,results=requirement_checks))
    write_json(directory/'source_evidence.json',dict(meta,events=source_evidence))
    conformance=check_conformance(model,contract,requirements)
    write_json(directory/'ir_conformance.json',dict(meta,**conformance))
    mapping={}
    if source_map_path and Path(source_map_path).is_file():
        mapping=json.loads(Path(source_map_path).read_text(encoding='utf-8'))
        if mapping.get('ir_hash')!=meta['ir_hash'] or mapping.get('rtl_hash')!=meta['rtl_hash']:
            raise ValueError('source map input hashes do not match this attempt')
        rtl_lines=Path(rtl_path).read_text(encoding='utf-8').splitlines()
        for region in mapping['regions']:
            region['code']='\n'.join(rtl_lines[region['line_start']-1:region['line_end']])
    g0=build_graph(model,requirements,assumptions,mapping,dict(meta,snapshot_id='post_rtl_pre_validation'))
    g0=with_evidence(g0,source_evidence,'post_rtl_pre_validation')
    if property_path:
        from .properties import attach_properties,load_available_properties
        g0=attach_properties(g0,load_available_properties(ir_path,property_path,directory),visibility='G0')
    write_json(directory/'graph_G0.json',g0)
    local=[];alternatives=[];validations=[]
    for assumption in assumptions:
        executed=0
        for alternative in assumption['alternatives']:
            try:
                if executable_assumption_ids is not None and assumption['id'] not in executable_assumption_ids:
                    raise Unsupported('outside predeclared offline candidate execution subset')
                if alternative['kind']=='behavior_change' and executed>=max_alternatives:
                    raise Unsupported('fixed candidate execution budget exhausted; candidate retained')
                revised,patch=typed_patch(model,assumption,alternative)
                executed+=1
                alt_directory=directory/'alternatives'/digest(alternative['id'])[:16]
                path,revised=save_alternative(revised,patch,alt_directory)
                result=local_difference(model,revised,assumption['id'],alternative['id'],budget_ms=budget_ms,environment=environment)
                alternatives.append((assumption,alternative,revised,patch))
                validations.append(dict(alternative_id=alternative['id'],status='validated',ir_path=str(path),patch=patch))
            except (Unsupported,ValueError,RuntimeError,CompilerError) as exc:
                result=unknown(assumption['id'],alternative['id'],'local',exc)
                validations.append(dict(alternative_id=alternative['id'],status='unknown',reason=str(exc)))
            result.setdefault('visibility','G1')
            local.append(result)
    g1=with_evidence(g0,local,'post_local_witness')
    write_json(directory/'graph_G1.json',g1)
    write_json(directory/'candidate_validation.json',dict(meta,results=validations))
    write_json(directory/'witnesses.json',dict(meta,snapshot_id='post_local_witness',results=local))
    write_json(directory/'structural_cones.json',dict(meta,results=[dict(assumption_id=a['id'],**structural_cone(g0,a['bindings'])) for a in assumptions]))
    predictions=empty_predictions(assumptions,horizon)
    if checkpoint:
        from .predict import predict_graph
        predictions=predict_graph(g1,assumptions,checkpoint,horizon,encoder_path,calibrator_path=calibrator_path)
    for prediction in predictions:
        prediction['tool_results']=[dict(claim_kind=e['claim_kind'],status=e['status'],scope=e.get('scope'),
            interpretation_status=e.get('interpretation_status')) for e in source_evidence+local if e['assumption_id']==prediction['assumption_id']]
    write_json(directory/'predictions.json',dict(meta,snapshot_id='post_local_witness',predictions=predictions))
    coverage=dict(meta,status='analyzed',requirement_review=extraction['provider'] if spec_path else 'no_visible_spec',
        choices=len(assumptions),alternatives=len(local),supported_local=sum(r['status'] in ('sat','unsat') for r in local),
        bound_llm_candidates=sum(v['status']=='bound' for v in proposal_validation),
        rejected_llm_candidates=sum(v['status']=='rejected' for v in proposal_validation),
        exact_source_map=bool(mapping),dependency_complete=g0['coverage']['dependency_complete'],
        conformance_status=conformance['status'],model_status='predicted' if checkpoint else 'not_run')
    write_json(directory/'coverage.json',coverage)
    write_report(directory,meta,assumptions,local,[])
    return dict(directory=directory,metadata=meta,model=model,assumptions=assumptions,requirements=requirements,
        requirement_checks=requirement_checks,source_evidence=source_evidence,local=local,g1=g1,alternatives=alternatives,
        environment=environment,horizon=horizon,budget_ms=budget_ms,coverage=coverage,output_protocol=protocol,mapping=mapping)


def finish_analysis(state, validation, *, bounded=False, replay_enabled=True, timeout=120, property_path=None,
                    bounded_assumption_ids=None, replay_simulator='verilator'):
    directory,metadata=state['directory'],state['metadata'];results=[];evidence=[]
    for assumption,alternative,revised,patch in state['alternatives']:
        if not bounded:continue
        if bounded_assumption_ids is not None and assumption['id'] not in bounded_assumption_ids:continue
        result=bounded_impact(state['model'],revised,assumption['id'],alternative['id'],horizon=state['horizon'],
            budget_ms=state['budget_ms'],environment=state['environment'],output_protocol=state['output_protocol'])
        queries=result.get('queries',[])
        sat=next((q for q in queries if q['status']=='sat'),None)
        replay_result=dict(status='not_run',reason='disabled, no exact mapping, or no reachable SAT witness')
        if sat and replay_enabled and state['coverage']['exact_source_map']:
            from .replay import replay
            replay_result=replay(state['model'],revised,sat['witness'],directory/'replay'/digest(alternative['id'])[:16],timeout=timeout,simulator=replay_simulator)
        result['replay']=replay_result
        if replay_result['status']=='quarantined':result['status']='quarantined'
        for query in queries:
            query['replay_status']=replay_result['status'] if query is sat else 'not_run'
            if replay_result['status']=='quarantined':query.update(status='inconsistent',reason='associated RTL replay failed; evidence quarantined')
        result.update(assumption_id=assumption['id'],alternative_id=alternative['id'],patch=patch)
        results.append(result);evidence.extend(queries)
    g2=with_evidence(state['g1'],evidence,'post_validation');g2['validation']=validation
    if property_path and Path(property_path).is_file():
        import yaml
        from .properties import attach_properties
        g2=attach_properties(g2,yaml.safe_load(Path(property_path).read_text(encoding='utf-8')),visibility='G2',namespace='post_validation')
    write_json(directory/'graph_G2.json',g2)
    write_json(directory/'bounded_witnesses.json',dict(metadata,snapshot_id='post_validation',results=results))
    write_json(directory/'validation_evidence.json',dict(metadata,snapshot_id='post_validation',validation=validation))
    write_report(directory,metadata,state['assumptions'],state['local'],results,validation)
    summary=dict(state['coverage'],directory=str(directory.resolve()),
        graphs={s:str((directory/f'graph_G{i}.json').resolve()) for i,s in enumerate(('post_rtl_pre_validation','post_local_witness','post_validation'))},
        bounded_status='completed' if bounded else 'not_run',replay_enabled=replay_enabled,report=str((directory/'report.md').resolve()))
    write_json(directory/'analysis_manifest.json',summary)
    return summary
