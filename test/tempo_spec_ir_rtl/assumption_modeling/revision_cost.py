"""Measure a fixed future revision workflow. Edit counts are a separate deterministic proxy."""
import platform
import statistics
import time
from pathlib import Path
from Compiler.rtl_compiler.compiler import compile_ir
from Compiler.rtl_compiler.errors import CompilerError
from .alternatives import typed_patch,save_alternative
from .schema import digest,file_hash,write_json

POLICY_ID='typed_patch_checker_compile_bounded_v1'


def measure_revision(model,assumption,alternative,directory,*,horizon=4,budget_ms=2000,repeats=3,replay_enabled=False,timeout=120,environment=(),output_protocol=None):
    if repeats<1:raise ValueError('revision measurement repeats must be positive')
    from .witness import bounded_impact
    from iteration.verilator_runner import run_verilator
    directory=Path(directory);runs=[]
    scope=dict(policy_id=POLICY_ID,unit='seconds',horizon=horizon,budget_ms=budget_ms,
        replay_enabled=replay_enabled,semantics='two_state',
        platform=platform.platform(),processor=platform.processor(),
        compiler_hash=file_hash(Path(__file__).resolve().parents[1]/'Compiler/rtl_compiler/backend.py'),
        environment_hash=digest(environment),output_protocol_hash=digest(output_protocol))
    for index in range(repeats):
        work=directory/f'repeat_{index}';started=time.perf_counter()
        record=dict(repeat=index,status='failed',llm_tokens=0,llm_calls=0)
        try:
            revised,patch=typed_patch(model,assumption,alternative)
            path,revised=save_alternative(revised,patch,work)
            rtl=work/'generated_rtl.v';compile_ir(path,rtl,work/'compiler.json')
            after_edit=time.perf_counter()
            _,lint=run_verilator(rtl,work,lint_only=True,raise_on_error=False,artifact_prefix='revision_lint',timeout=timeout)
            bounded=bounded_impact(model,revised,assumption['id'],alternative['id'],horizon=horizon,budget_ms=budget_ms,
                environment=environment,output_protocol=output_protocol)
            checked=bounded.get('queries',[])
            complete=bool(checked) and all(q['status'] in ('sat','unsat') for q in checked)
            replay_status='not_run'
            sat=next((q for q in checked if q['status']=='sat'),None)
            if replay_enabled and sat:
                from .replay import replay
                replay_status=replay(model,revised,sat['witness'],work/'replay',timeout=timeout)['status']
                complete=complete and replay_status=='verified'
            finished=time.perf_counter()
            record.update(status='completed' if complete and lint.get('status')=='passed' else 'failed',
                revision_seconds=after_edit-started,verification_seconds=finished-after_edit,total_seconds=finished-started,
                semantic_edit_count=patch['semantic_edit_count'],lint_status=lint.get('status'),bounded_status=bounded['status'],replay_status=replay_status)
        except (ValueError,RuntimeError,KeyError,ImportError,CompilerError) as exc:
            record.update(reason=str(exc),total_seconds=time.perf_counter()-started)
        runs.append(record)
    completed=[r['total_seconds'] for r in runs if r['status']=='completed']
    result=dict(schema_version='c1-revision-cost-1',assumption_id=assumption['id'],alternative_id=alternative['id'],
        scope=scope,runs=runs,status='measured' if len(completed)==repeats else 'incomplete',
        value=statistics.median(completed) if len(completed)==repeats else None,
        dispersion_seconds=statistics.pstdev(completed) if completed else None,
        source='executed_revision_and_verification',semantic_edit_count=runs[0].get('semantic_edit_count'),
        tokens=0,measured_at='oracle_only')
    write_json(directory/'measurement.json',result)
    return result
