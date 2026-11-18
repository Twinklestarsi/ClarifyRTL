#!/usr/bin/env python3
"""Check equivalence of DAG-flow RTL against each Tempo reference."""
import hashlib, json, sys, pathlib
sys.path.insert(0, 'experiments/ir_question_dag'); sys.path.insert(0, '.')
from blocks.b1_scaffold import get_case, load_config, load_cases, sanitize_network_env
from blocks.b7_phase_c.equivalence import check_equivalence

sanitize_network_env(); cfg = load_config()
root = pathlib.Path('experiments/ir_question_dag/runs/_dag_tempo_all')
cases = {hashlib.sha256(c.prompt_text().strip().encode()).hexdigest(): c
         for c in load_cases(cfg['run']['case_root'], dataset='Tempo26')}
rows = []
for d in sorted(p for p in root.iterdir() if p.is_dir()):
    spec, rtl = d/'spec.md', d/'generated_rtl.v'
    if not spec.exists():
        rows.append((d.name, '?', 'no spec', None)); continue
    case = cases.get(hashlib.sha256(spec.read_text().strip().encode()).hexdigest())
    if case is None:
        rows.append((d.name, '?', 'unmatched', None)); continue
    if not rtl.exists():
        rows.append((case.short, case.short, 'no rtl', None)); continue
    verdict = check_equivalence(case, rtl, d/'formal_check', cfg['formal'])
    rows.append((case.short, case.short, verdict.get('status'), len(rtl.read_text().splitlines())))
    print(f"{case.short:14s} {verdict.get('status'):16s} lines={rows[-1][3]}", flush=True)
out = pathlib.Path('experiments/ir_question_dag/runs/_batch/dag_tempo_equiv.json')
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
print('total:', len(rows), 'equivalent:', sum(1 for r in rows if r[2] == 'equivalent'))
