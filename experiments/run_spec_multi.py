#!/usr/bin/env python3
"""Run spec-LLM-RTL on several case subsets, streaming per-label results."""
from __future__ import annotations
import argparse, json, subprocess, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE=Path(__file__).resolve().parent; RUNNER=HERE/'runner_cases'; ADAPTERS=HERE/'adapters'
PY='/nas2nfs/homes/2531930/ONE-TURN/dac2026_c1/.venv_c1/bin/python'
OUT=HERE/'runs'/'spec_multi'
NAME={'deepseek_aic':'DeepSeek-V4.1-Flash (AIC)','qwen':'Qwen3.8-27B (AIC)'}

def cases(label):
    man=json.loads((RUNNER/'manifest.json').read_text())
    return sorted([c for c in man['cases'] if c['dataset']==label],key=lambda c:c['short'])

def one(label,model,case):
    short=case['short']; out=OUT/label/model/short; res=out/'result.json'
    if res.is_file():
        try:
            d=json.loads(res.read_text())
            if d.get('status') in ('passed','failed','compile_failed'):
                return {**d,'_label':label,'_model':model,'_cached':True}
        except Exception: pass
    out.mkdir(parents=True,exist_ok=True)
    cmd=[PY,'run_spec_llm.py','--case',f'{label}/{short}','--model',model,'--out',str(out)]
    t=time.time()
    with (out/'run.log').open('w') as fh:
        proc=subprocess.run(cmd,cwd=str(ADAPTERS),stdout=fh,stderr=subprocess.STDOUT,timeout=1800)
    d={}
    if res.is_file():
        try: d=json.loads(res.read_text())
        except Exception: pass
    d.setdefault('status','error'); d['_label']=label; d['_model']=model
    d['_wall']=round(time.time()-t,1); d['_rc']=proc.returncode
    return d

def report(rows,labels,models):
    lines=['# spec-LLM-RTL: additional datasets','',f"Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}",'']
    lines+=['## Summary','','| dataset | model | done | passed | failed | compile_failed | pass@1 | avg tokens |','|---|---|---:|---:|---:|---:|---:|---:|']
    for lab in labels:
        for m in models:
            rs=[r for r in rows if r.get('_label')==lab and r.get('_model')==m]
            c={k:sum(1 for r in rs if r.get('status')==k) for k in ('passed','failed','compile_failed','error')}
            n=len(rs); p=c['passed']
            toks=[(r.get('llm_usage') or {}).get('total_tokens') or 0 for r in rs]
            avg=sum(toks)/len(toks) if toks else 0
            lines.append(f"| {lab} | {NAME.get(m,m)} | {n} | {p} | {c['failed']} | {c['compile_failed']} | {100*p/n if n else 0:.1f}% | {avg:,.0f} |")
    lines+=['','## Per-case','']
    by={(r.get('_label'),r.get('_model'),r.get('case')):r for r in rows}
    for lab in labels:
        lines+= [f'### {lab}','','| case | '+' | '.join(NAME.get(m,m) for m in models)+' |','|---|'+'---|'*len(models)]
        for c in cases(lab):
            cells=[]
            for m in models:
                r=by.get((lab,m,f'{lab}/{c["short"]}'))
                cells.append('—' if not r else (r.get('status') or 'error'))
            lines.append(f"| {c['short']} | "+' | '.join(cells)+' |')
        lines.append('')
    (OUT/'RESULTS.md').write_text('\n'.join(lines)+'\n')

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--labels',default='RTLLM_AMBIG_49,VERILOGEVAL_49,VERILOGEVAL_AMBIG_49')
    ap.add_argument('--models',default='deepseek_aic,qwen')
    ap.add_argument('--workers',type=int,default=6)
    a=ap.parse_args(); labels=[x for x in a.labels.split(',') if x]; models=[x for x in a.models.split(',') if x]
    tasks=[(l,m,c) for l in labels for m in models for c in cases(l)]
    OUT.mkdir(parents=True,exist_ok=True); prog=OUT/'progress.jsonl'; rows=[]
    print(f'tasks={len(tasks)}',flush=True)
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs={pool.submit(one,*t):t for t in tasks}
        for i,f in enumerate(as_completed(futs),1):
            l,m,c=futs[f]
            try: d=f.result()
            except Exception as e: d={'status':'error','error':f'{type(e).__name__}: {e}','_label':l,'_model':m}
            rows.append(d)
            with prog.open('a') as fh: fh.write(json.dumps({k:v for k,v in d.items() if not k.startswith('_')},ensure_ascii=False)+'\n')
            print(f"[{i}/{len(tasks)}] {l}/{m}/{c['short']} {d.get('status')} {(d.get('llm_usage') or {}).get('total_tokens')} tok",flush=True)
            if i%2==0 or i==len(tasks): report(rows,labels,models)
    report(rows,labels,models); print('wrote',OUT/'RESULTS.md')

if __name__=='__main__': main()
