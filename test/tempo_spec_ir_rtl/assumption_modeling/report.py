"""Readable Contribution 1 outputs, with facts, scopes and learned scores kept separate."""
import json
from pathlib import Path


def _cell(value):
    return str(value).replace('|','\\|').replace('\n',' ')


def write_report(directory,metadata,assumptions,local,bounded,validation=None):
    directory=Path(directory)
    def load(name,default):
        path=directory/name
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default
    lines=['# Contribution 1: cross-representation assumption modeling','',
        f"IR hash: `{metadata.get('ir_hash')}`",f"RTL hash: `{metadata.get('rtl_hash')}`",'',
        'A matching quotation verifies provenance, not the correctness of a semantic interpretation. '
        'A difference witness does not establish user intent. Model probabilities do not confirm requirements.','',
        '## Current design commitments','',
        '| Choice | Current interpretation | Binding | Source/evidence status | Related requirements |',
        '|---|---|---|---|---|']
    for a in assumptions:
        lines.append('| '+' | '.join(_cell(v) for v in (a['id'],a['current_interpretation'],a['binding_state'],a.get('evidence_status','needs_source_review'),', '.join(a.get('related_requirement_ids',[]))))+' |')
    lines+=['','`needs_source_review` is not a proof that the whole specification omits a requirement.','',
        '## Local behavioral differences','',
        '| Alternative | Result | Scope / unknown reason |','|---|---|---|']
    for result in local:
        lines.append('| '+' | '.join(_cell(v) for v in (result['alternative_id'],result['status'],result.get('reason') or result.get('scope',{})))+' |')
    for result in local:
        if result.get('witness'):
            lines += ['',f"Witness for `{result['alternative_id']}` (abstract state; not a reachable trace):",'```json',json.dumps(result['witness'],ensure_ascii=False,indent=2),'```']
    lines+=['','## Bounded impact and RTL replay','',
        'Each semantic observation has its own query. UNSAT means no difference within the declared K and environment, not unbounded equivalence. '
        'Mapped RTL labels are IR-supported localization, not independent RTL formal proof.','',
        '| Alternative | K | SAT observations | UNSAT observations | Other | Replay |','|---|---|---|---|---|---|']
    for result in bounded:
        queries=result.get('queries',[])
        values=(result['alternative_id'],result.get('horizon'),sum(q['status']=='sat' for q in queries),sum(q['status']=='unsat' for q in queries),sum(q['status'] not in ('sat','unsat') for q in queries),result.get('replay',{}).get('status','not_run'))
        lines.append('| '+' | '.join(_cell(v) for v in values)+' |')
    if not bounded:lines+=['','Bounded checks were not executed.']
    predictions=load('predictions.json',{}).get('predictions',[])
    if any(p.get('pilot_only') for p in predictions):
        lines+=['','Model scope: machine-checked controlled pilot; labels have not received independent human review.']
    lines+=['','## Learned predictions','',
        'Absent checkpoints or unsupervised heads produce null outputs. These are separate from the source/tool facts above.','',
        '| Alternative | Mismatch probability | Observable-difference probability | Revision cost | Calibration |','|---|---|---|---|---|']
    for prediction in predictions:
        cost=prediction.get('predicted_revision_cost')
        cost_text=f"{cost['value']:.6g} {cost['unit']} ({cost.get('policy_id')})" if cost else None
        values=(prediction['alternative_id'],prediction.get('predicted_mismatch_probability'),prediction.get('predicted_observable_difference_probability'),cost_text,prediction.get('calibration','not_run'))
        lines.append('| '+' | '.join(_cell(v) for v in values)+' |')
    lines+=['','## Artifacts','']
    for name in ('requirements.json','assumptions.json','requirement_checks.json','source_evidence.json','proposal_validation.json',
                 'source_map.json','candidate_validation.json','witnesses.json','bounded_witnesses.json','structural_cones.json',
                 'predictions.json','coverage.json','graph_G0.json','graph_G1.json','graph_G2.json'):
        if (directory/name).is_file():lines.append(f'- [{name}]({(directory/name).resolve()})')
    lines+=['','## Pipeline validation','', '```json',json.dumps(validation or dict(status='not_run'),ensure_ascii=False,indent=2),'```','']
    path=directory/'report.md';path.write_text('\n'.join(lines),encoding='utf-8')
    return str(path)
