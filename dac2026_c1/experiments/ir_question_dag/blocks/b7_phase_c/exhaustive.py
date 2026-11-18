"""Exhaustive equivalence for small combinational designs.

For a combinational module, enumerating every input assignment *is* a proof of
equivalence.  JasperGold handles these cases slowly and is sensitive to ``x``
don't-care outputs, so this check runs first when the port list has no clock and
the input space is small enough.
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


def _module_name(path: Path) -> str | None:
    match = _MODULE.search(path.read_text(errors="replace"))
    return match.group(1) if match else None


def is_combinational(ports: list[dict]) -> bool:
    names = {port["name"].lower() for port in ports}
    return not any(name in names for name in ("clk", "clock", "clk_i", "aclk"))


def input_bits(ports: list[dict]) -> int:
    return sum(int(port.get("width", 1)) for port in ports if port["direction"] == "input")


def build_tb(ports: list[dict], golden: str, candidate: str) -> tuple[str, int]:
    inputs = [port for port in ports if port["direction"] == "input"]
    outputs = [port for port in ports if port["direction"] == "output"]
    bits = input_bits(ports)
    lines = ["`timescale 1ns/1ps", "module tb;"]
    for port in inputs:
        span = f"[{port['width'] - 1}:0] " if int(port.get("width", 1)) > 1 else ""
        lines.append(f"  reg {span}{port['name']};")
    for port in outputs:
        span = f"[{port['width'] - 1}:0] " if int(port.get("width", 1)) > 1 else ""
        lines.append(f"  wire {span}g_{port['name']};")
        lines.append(f"  wire {span}d_{port['name']};")
    specs = ", ".join(f".{p['name']}({p['name']})" for p in inputs)
    outs_g = ", ".join(f".{p['name']}(g_{p['name']})" for p in outputs)
    outs_d = ", ".join(f".{p['name']}(d_{p['name']})" for p in outputs)
    sep = ", " if specs and outs_g else ""
    lines.append(f"  {golden} u_gold ({specs}{sep}{outs_g});")
    lines.append(f"  {candidate} u_dut ({specs}{sep}{outs_d});")
    lines.append("  integer i;")
    lines.append("  integer errors = 0;")
    lines.append("  initial begin")
    vector = "{" + ", ".join(p["name"] for p in inputs) + "}" if len(inputs) > 1 else inputs[0]["name"]
    lines.append(f"    for (i = 0; i < {1 << bits}; i = i + 1) begin")
    lines.append(f"      {vector} = i[{bits - 1}:0];")
    lines.append("      #1;")
    for port in outputs:
        name = port["name"]
        width = int(port.get("width", 1))
        if width == 1:
            condition = f"(!((g_{name} === d_{name}) || (g_{name} === 1'bx)))"
        else:
            condition = "(" + " || ".join(
                f"(!((g_{name}[{bit}] === d_{name}[{bit}]) || (g_{name}[{bit}] === 1'bx)))"
                for bit in range(width)) + ")"
        lines.append(f"      if ({condition}) begin")
        lines.append(f'        if (errors < 3) $display("MISMATCH i=%0d {name} golden=%0h dut=%0h",'
                     f" i, g_{name}, d_{name});")
        lines.append("        errors = errors + 1;")
        lines.append("      end")
    lines.append("    end")
    lines.append('    if (errors == 0) $display("EXHAUSTIVE_OK");')
    lines.append('    else $display("EXHAUSTIVE_FAIL %0d", errors);')
    lines.append("    $finish;")
    lines.append("  end")
    lines.append("endmodule")
    return "\n".join(lines) + "\n", bits


def exhaustive_equivalence(case, candidate_rtl: Path | str, out_dir: Path,
                           formal: dict[str, Any], *, max_bits: int = 16,
                           timeout: int = 120) -> dict[str, Any]:
    from blocks.b7_phase_c.equivalence import _pair, _shared

    shared = _shared()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        info = shared.prepare_verification(_pair(case), Path(candidate_rtl).read_text(encoding="utf-8"),
                                           {}, out_dir)
    except Exception as exc:  # noqa: BLE001
        return {"status": "prepare_failed", "reason": f"{type(exc).__name__}: {exc}"[:300]}
    if info.get("status") != "ready":
        return {"status": "prepare_failed", "reason": str(info.get("reason", ""))[:300]}
    ports = info["golden_ports"]
    if not is_combinational(ports):
        return {"status": "not_applicable", "reason": "sequential design"}
    bits = input_bits(ports)
    if bits > max_bits:
        return {"status": "not_applicable", "reason": f"{bits} input bits exceed {max_bits}"}
    golden = _module_name(out_dir / "golden.sv")
    candidate = _module_name(out_dir / "candidate.sv")
    if not golden or not candidate:
        return {"status": "prepare_failed", "reason": "module name not found"}
    tb, bits = build_tb(ports, golden, candidate)
    (out_dir / "tb_exhaustive.sv").write_text(tb, encoding="utf-8")
    binary = out_dir / "exh.vvp"
    build = subprocess.run([str(IVERILOG), "-g2012", "-o", str(binary),
                            str(out_dir / "golden.sv"), str(out_dir / "candidate.sv"),
                            str(out_dir / "tb_exhaustive.sv")],
                           capture_output=True, text=True, timeout=timeout)
    if build.returncode != 0:
        return {"status": "compile_error", "reason": (build.stderr or "")[-500:]}
    run = subprocess.run([str(VVP), str(binary)], capture_output=True, text=True, timeout=timeout)
    output = (run.stdout or "") + (run.stderr or "")
    (out_dir / "exhaustive.log").write_text(output[-4000:], encoding="utf-8")
    if "EXHAUSTIVE_OK" in output:
        return {"status": "equivalent", "method": "exhaustive", "vectors": 1 << bits}
    mismatches = [line for line in output.splitlines() if line.startswith("MISMATCH")]
    if mismatches:
        return {"status": "counterexample", "method": "exhaustive",
                "vectors": 1 << bits, "first": mismatches[0][:300]}
    return {"status": "sim_error", "reason": output[-300:]}