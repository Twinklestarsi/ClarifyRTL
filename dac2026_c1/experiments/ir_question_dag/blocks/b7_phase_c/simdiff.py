"""Bounded random-stimulus differential simulation.

JasperGold only returns ``cex`` without a trace, so the repair prompt cannot
see *which input* diverges.  This module builds a testbench that instantiates
the namespaced golden and candidate modules from the equivalence harness,
drives random vectors, and reports the first mismatch with the input values.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[4]
IVERILOG = PROJECT_ROOT / ".tools_c1" / "bin" / "iverilog"
VVP = PROJECT_ROOT / ".tools_c1" / "bin" / "vvp"

_MODULE = re.compile(r"\bmodule\s+([A-Za-z_][\w$]*)")
_CLOCK = re.compile(r"clock\s*\{([^}]+)\}")


def _module_name(path: Path) -> str | None:
    match = _MODULE.search(path.read_text(errors="replace"))
    return match.group(1) if match else None


def _decl(port: dict, kind: str) -> str:
    span = f"[{port['width'] - 1}:0] " if int(port.get("width", 1)) > 1 else ""
    return f"  {kind} {span}{port['name']};"


def build_testbench(ports: list[dict], golden_module: str, candidate_module: str,
                    clock: str | None, reset_expression: str, *,
                    cycles: int = 64, seed: int = 1) -> str:
    inputs = [port for port in ports if port["direction"] == "input"]
    outputs = [port for port in ports if port["direction"] == "output"]
    clocks = [clock] if clock else []
    resets = [name for name in re.findall(r"[A-Za-z_][\w$]*", reset_expression)
              if name in {port["name"] for port in inputs}]
    data_inputs = [port for port in inputs if port["name"] not in clocks + resets]
    lines = ["`timescale 1ns/1ps", "module tb;"]
    for port in inputs:
        lines.append(_decl(port, "reg"))
    for port in outputs:
        lines.append(_decl(port, "wire") + f"  // golden view: g_{port['name']}")
    probe = " ".join(f"{port['name']}=%0h" for port in data_inputs)
    probe_args = ", ".join(port['name'] for port in data_inputs)
    probe_fmt = (probe + " ") if probe else ""
    probe_vals = (probe_args + ", ") if probe_args else ""
    specs = ", ".join(f".{port['name']}({port['name']})" for port in inputs)
    outs_g = ", ".join(f".{port['name']}(g_{port['name']})" for port in outputs)
    outs_d = ", ".join(f".{port['name']}(d_{port['name']})" for port in outputs)
    for port in outputs:
        span = f"[{port['width'] - 1}:0] " if int(port.get("width", 1)) > 1 else ""
        lines.append(f"  wire {span}g_{port['name']};")
        lines.append(f"  wire {span}d_{port['name']};")
    lines.append(f"  {golden_module} u_gold ({specs}{', ' if specs and outs_g else ''}{outs_g});")
    lines.append(f"  {candidate_module} u_dut ({specs}{', ' if specs and outs_d else ''}{outs_d});")
    lines.append("  integer cycle;")
    lines.append(f"  integer rnd = {seed};")
    lines.append("  initial begin")
    for port in inputs:
        lines.append(f"    {port['name']} = 0;")
    lines.append(f"    for (cycle = 0; cycle < {cycles}; cycle = cycle + 1) begin")
    if clocks:
        lines.append(f"      {clocks[0]} = 0;")
    for port in data_inputs:
        lines.append(f"      {port['name']} = $random(rnd);")
    if resets:
        for name in resets:
            active_low = f"!{name}" in reset_expression or f"~{name}" in reset_expression
            hold = "1'b0" if active_low else "1'b1"
            idle = "1'b1" if active_low else "1'b0"
            lines.append(f"      {name} = (cycle < 2) ? {hold} : {idle};")
    if clocks:
        lines.append("      #5;")
        lines.append(f"      {clocks[0]} = 1;")
        lines.append("      #4;")
        for port in outputs:
            lines.append(f"      if (g_{port['name']} !== d_{port['name']}) begin")
            lines.append(f'        $display("MISMATCH cycle=%0d {probe_fmt}golden=%0h dut=%0h",'
                         f' cycle, {probe_vals}g_{port["name"]}, d_{port["name"]});')
            lines.append("        $finish;")
            lines.append("      end")
        lines.append("      #1;")
        lines.append(f"      {clocks[0]} = 0;")
        lines.append("      #5;")
    else:
        lines.append("      #1;")
        for port in outputs:
            lines.append(f"      if (g_{port['name']} !== d_{port['name']}) begin")
            lines.append(f'        $display("MISMATCH cycle=%0d {probe_fmt}golden=%0h dut=%0h",'
                         f' cycle, {probe_vals}g_{port["name"]}, d_{port["name"]});')
            lines.append("        $finish;")
            lines.append("      end")
    lines.append("    end")
    lines.append('    $display("NO_MISMATCH");')
    lines.append("    $finish;")
    lines.append("  end")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def simulate_diff(case, candidate_rtl: Path | str, out_dir: Path,
                  formal: dict[str, Any], *, cycles: int = 48,
                  seed: int = 1, timeout: int = 90) -> dict[str, Any]:
    from blocks.b7_phase_c.equivalence import _pair, _shared

    shared = _shared()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate_text = Path(candidate_rtl).read_text(encoding="utf-8")
    try:
        from blocks.b7_phase_c.equivalence import _reset_expression

        reset = _reset_expression(case)
        settings = {"reset_expression": reset} if reset else {}
        info = shared.prepare_verification(_pair(case), candidate_text, settings, out_dir)
    except Exception as exc:  # noqa: BLE001
        return {"status": "prepare_failed", "reason": f"{type(exc).__name__}: {exc}"[:300]}
    if info.get("status") != "ready":
        return {"status": "prepare_failed", "reason": info.get("reason", "")[:300]}

    golden_module = _module_name(out_dir / "golden.sv")
    candidate_module = _module_name(out_dir / "candidate.sv")
    if not golden_module or not candidate_module:
        return {"status": "prepare_failed", "reason": "module name not found"}

    plan = info.get("formal_plan") or {}
    clock = None
    for command in plan.get("clock_tcl") or []:
        match = _CLOCK.search(command)
        if match:
            clock = match.group(1).strip()
    reset_expression = plan.get("reset_expression") or ""
    tb = build_testbench(info["golden_ports"], golden_module, candidate_module,
                         clock, reset_expression, cycles=cycles, seed=seed)
    (out_dir / "tb.sv").write_text(tb, encoding="utf-8")
    binary = out_dir / "sim.vvp"
    try:
        build = subprocess.run([str(IVERILOG), "-g2012", "-o", str(binary),
                                str(out_dir / "golden.sv"), str(out_dir / "candidate.sv"),
                                str(out_dir / "tb.sv")],
                               capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "compile_error", "reason": str(exc)[:300]}
    if build.returncode != 0:
        return {"status": "compile_error", "reason": (build.stderr or "")[-600:]}
    try:
        run = subprocess.run([str(VVP), str(binary)], capture_output=True, text=True,
                             timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "sim_error", "reason": str(exc)[:300]}
    output = (run.stdout or "") + (run.stderr or "")
    mismatch = [line for line in output.splitlines() if line.startswith("MISMATCH")]
    (out_dir / "sim.log").write_text(output[-4000:], encoding="utf-8")
    if mismatch:
        return {"status": "mismatch", "cycles": cycles, "first": mismatch[0][:400],
                "lines": mismatch[:5]}
    if "NO_MISMATCH" in output:
        return {"status": "no_mismatch", "cycles": cycles}
    return {"status": "sim_error", "reason": output[-400:]}