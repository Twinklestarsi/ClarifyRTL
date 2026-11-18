"""Opt-in, file-resumable pre-IR analysis, initially for clear/load priority."""
import json
from pathlib import Path
from .dag_validator import validate_dag
from .requirements import extract_requirements, review_priority
from .schema import envelope, digest, write_json


def analyze_before_ir(spec_path, output_dir, answers_path=None):
    text = Path(spec_path).read_text(encoding='utf-8')
    meta = envelope(spec_path=spec_path, snapshot_id='pre_ir')
    requirements = extract_requirements(text)
    dag = dict(roots=[], nodes=[], edges=[])
    evidence, constraints, environment, freedoms, requests = [], [], [], [], []
    import re
    applicable = all(re.search(rf'\b{name}\b', text, re.I) for name in ('clear', 'load'))
    answers = {}
    if answers_path:
        doc = json.loads(Path(answers_path).read_text(encoding='utf-8'))
        if doc.get('spec_hash') != meta['spec_hash']:
            raise ValueError('Clarification answers belong to a different specification hash')
        for answer in doc.get('answers', []):
            if answer.get('question_id') != 'DEC_COLLISION_POLICY' or not applicable:
                raise ValueError('Unknown clarification question ID')
            if answer['question_id'] in answers or not answer.get('source'):
                raise ValueError('Duplicate answer or missing answer source')
            answers[answer['question_id']] = answer
    if applicable:
        claims, free_sources = review_priority(requirements)
        domain = ['clear_first', 'load_first', 'exclusive_inputs', 'other']
        node = dict(id='DEC_COLLISION_POLICY', kind='decision',
                    question='clear 与 load 同时有效时，应采用什么行为？', value_domain=domain,
                    domain_closed=False, resolution_state='unresolved', selected_value=None,
                    requirement_ids=[r['id'] for r in requirements if re.search(r'\b(clear|load)\b', r['text'], re.I)],
                    evidence_ids=[])
        dag['roots'].append(node['id'])
        dag['nodes'].append(node)
        for value in domain:
            target = 'OUT_' + value
            dag['nodes'].append(dict(id=target, kind='requires_refinement' if value == 'other' else 'constraint_fragment', template=value))
            dag['edges'].append(dict(source=node['id'], target=target, value=value, origin='candidate_interpretation'))
        values = {c['value'] for c in claims}
        evidence.extend(dict(id='EV_' + c['requirement_id'], kind='specification', **c) for c in claims)
        answer = answers.get(node['id'])
        if answer:
            if answer.get('value') not in domain:
                raise ValueError('Clarification value outside the declared domain')
            evidence.append(dict(id='EV_USER_001', kind='user_clarification', **answer))
            values.add(answer['value'])
        if len(values) > 1 or (free_sources and values):
            node['resolution_state'] = 'evidence_conflict'
        elif values:
            selected = next(iter(values))
            node['selected_value'] = selected
            node['resolution_state'] = 'specified' if selected != 'other' else 'requires_refinement'
            ids = [ev['id'] for ev in evidence]
            if selected in ('clear_first', 'load_first'):
                high, low = ('clear', 'load') if selected == 'clear_first' else ('load', 'clear')
                constraints.append(dict(id='CON_CLEAR_LOAD', kind='priority', higher_operation=high,
                                        lower_operation=low, scope='normal_operation', evidence_ids=ids))
            elif selected == 'exclusive_inputs':
                environment.append(dict(id='ENV_CLEAR_LOAD', expression='!(clear && load)', evidence_ids=ids,
                                        signals=['clear', 'load']))
            # Other requires typed behavior refinement, never silently generates.
        elif free_sources:
            node['resolution_state'] = 'implementation_freedom'
            freedoms.append(dict(id='FREE_CLEAR_LOAD', kind='priority', allowed_values=domain[:2],
                                 requirement_ids=free_sources))
        if node['resolution_state'] in ('unresolved', 'requires_refinement', 'evidence_conflict'):
            requests.append(dict(question_id=node['id'], question=node['question'], choices=domain,
                                 reason=node['resolution_state'], required=True,
                                 proposed_scenario=dict(clear=1, load=1, data_in=5)))
        node['evidence_ids'] = [e['id'] for e in evidence]
    validation = validate_dag(dag, [r['id'] for r in requirements])
    if validation['status'] != 'valid':
        raise ValueError(str(validation['errors']))
    status = 'evidence_conflict' if any(q['reason'] == 'evidence_conflict' for q in requests) else 'needs_clarification' if requests else 'ready'
    contract = dict(meta, status=status, constraints=constraints, environment_constraints=environment,
                    implementation_freedoms=freedoms, unresolved_required=requests,
                    coverage=dict(supported=['clear_load_priority'], other_decisions='not_analyzed'))
    version = digest(dict(spec_hash=meta['spec_hash'], evidence=evidence))[:16]
    directory = Path(output_dir) / version
    paths = {}
    artifacts = dict(requirements=requirements, condition_dag=dag, dag_validation_report=validation,
                     evidence=evidence, generation_contract=contract, assumptions=dag['nodes'],
                     clarification_requests=requests)
    for name, payload in artifacts.items():
        body = payload if name == 'generation_contract' else dict(meta, **{name: payload})
        paths[name] = write_json(directory / f'{name}.json', body)
    state = dict(meta, status=status, version=version, paths=paths, unresolved_count=len(requests),
                 answer_template=dict(spec_hash=meta['spec_hash'], answers=[dict(question_id=q['question_id'], value=None, source='user') for q in requests]))
    write_json(directory / 'analysis_state.json', state)
    write_json(Path(output_dir) / 'latest.json', state)
    report = directory / 'pre_ir_report.md'
    report.write_text(f'# Pre-IR analysis\n\nStatus: {status}\n\nCoverage: clear/load priority only; other design decisions are not analyzed.\n\n' + '\n'.join(f'- {q["question"]} ({q["reason"]})' for q in requests) + '\n', encoding='utf-8')
    return dict(status=status, contract=contract, state=state, directory=str(directory.resolve()))
