"""Replay both implementations with solver-provided expected state sequences."""
from pathlib import Path
from Compiler.rtl_compiler.backend import generate_verilog
from iteration.verilator_runner import run_verilator
from .schema import write_json


def replay(current, alternative, witness, directory, *, timeout=120, simulator='verilator'):
    if simulator not in ('verilator','iverilog'):raise ValueError('unsupported replay simulator')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if not witness or 'frames' not in witness:
        return dict(status='not_run', reason='no reachable witness trace')
    import copy
    a, b = copy.deepcopy(current), copy.deepcopy(alternative)
    a.module_name, b.module_name = 'c1_current', 'c1_alternative'
    rtl_a, rtl_b = directory/'current.v', directory/'alternative.v'
    rtl_a.write_text(generate_verilog(a), encoding='utf-8')
    rtl_b.write_text(generate_verilog(b), encoding='utf-8')
    domain = a.clock_domains[0]
    active = 1 if domain.edge == 'posedge' else 0
    lines = ['module c1_replay_tb;']
    for port in a.ports:
        if port.direction == 'input':
            width = int(port.width)
            lines.append(f"reg [{width-1}:0] {port.name} = {1-active if port.name == domain.clock else 0};")
    for name in ('a', 'b'):
        module = a if name == 'a' else b
        connections = ', '.join(f'.{p.name}({p.name})' if p.direction == 'input' else f'.{p.name}()' for p in a.ports)
        lines.append(f'{module.module_name} dut_{name} ({connections});')
    lines.append('initial begin')
    def cycle(inputs, expected_a, expected_b):
        for name, value in inputs.items():
            lines.append(f"  {name} = {int(next(p.width for p in a.ports if p.name == name))}'d{value};")
        lines.append(f'  #5; {domain.clock} = {active}; #1;')
        for name, expected in (('a', expected_a), ('b', expected_b)):
            for state, value in expected.items():
                width = int(a.state_map()[state].width)
                lines.append(f'  if (dut_{name}.{state} !== {width}\'d{value}) $fatal(1, "C1_REPLAY_MISMATCH {name} {state}");')
        lines.append(f'  #4; {domain.clock} = {1-active};')
    if witness.get('reset_prefix') is not None:
        cycle(witness['reset_prefix'], witness['initial_A'], witness['initial_B'])
    for frame in witness['frames']:
        cycle(frame['inputs'], frame['next_A'], frame['next_B'])
    lines += ['  $display("C1_REPLAY_OK"); $finish;', 'end', 'endmodule']
    tb = directory/'replay_tb.sv'
    tb.write_text('\n'.join(lines)+'\n', encoding='utf-8')
    if simulator=='iverilog':
        import subprocess
        binary=directory/'replay.vvp';compile_log=directory/'replay_compile.log';simulation_log=directory/'replay_simulation.log'
        try:
            built=subprocess.run(['iverilog','-g2012','-s','c1_replay_tb','-o',str(binary),str(rtl_a),str(rtl_b),str(tb)],
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=timeout)
            compile_log.write_text(built.stdout)
            ran=subprocess.run(['vvp',str(binary)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=timeout) if built.returncode==0 else None
            simulation_log.write_text(ran.stdout if ran else '')
            report=dict(tool='iverilog',mode='simulation',status='passed' if ran and ran.returncode==0 else 'failed',
                compile_returncode=built.returncode,simulation_returncode=ran.returncode if ran else None,
                compile_log=str(compile_log),simulation_log=str(simulation_log))
        except (subprocess.TimeoutExpired,FileNotFoundError) as exc:
            report=dict(tool='iverilog',mode='simulation',status='failed',reason=str(exc))
    else:
        _, report = run_verilator(rtl_a, directory, testbenches=[tb], extra_sources=[rtl_b],
                                 tb_top='c1_replay_tb', assertions=False, timeout=timeout,
                                 artifact_prefix='replay', raise_on_error=False)
    passed = report.get('status') == 'passed' and report.get('mode') == 'simulation'
    if passed:
        passed = 'C1_REPLAY_OK' in Path(report['simulation_log']).read_text(encoding='utf-8')
    result = dict(status='verified' if passed else 'quarantined', report=report,
                  reason=None if passed else 'RTL replay did not confirm SMT expected values')
    write_json(directory/'replay_result.json', result)
    return result
