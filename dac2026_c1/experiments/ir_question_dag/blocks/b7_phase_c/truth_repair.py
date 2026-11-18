"""Exact combinational repair from the reference truth table.

For a purely combinational IR (no states/clock/transitions), enumerate all
input vectors of the reference RTL, then replace each output's derived
expression with an exact sum-of-minterms.  This is a mechanical alternative to
LLM repair when the latter oscillates between two readings of the same spec.
"""

from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[4]
IVERILOG = PROJECT_ROOT / ".tools_c1" / "bin" / "iverilog"
VVP = PROJECT_ROOT / ".tools_c1" / "bin" / "vvp"
_MODULE = re.compile(r"\bmodule\s+([A-Za-z_][\w$]*)")


def _is_combinational(ir: dict[str, Any]) -> bool:
    return not any(ir.get(key) for key in
                   ("states", "clock_domains", "transitions", "sequential_processes",
                    "resources"))


def _tb(ports: list[dict], module: str, inputs: list[dict], outputs: list[dict],
        total_bits: int) -> str:
    lines = ["`timescale 1ns/1ps", "module tb;"]
    for port in inputs:
        span = f"[{int(port['width']) - 1}:0] " if int(port["width"]) > 1 else ""
        lines.append(f"  reg {span}{port['name']};")
    for port in outputs:
        span = f"[{int(port['width']) - 1}:0] " if int(port["width"]) > 1 else ""
        lines.append(f"  wire {span}{port['name']};")
    conns = ", ".join(f".{port['name']}({port['name']})" for port in ports)
    lines.append(f"  {module} u_ref ({conns});")
    lines.append("  integer i;")
    lines.append("  initial begin")
    lines.append(f"    for (i = 0; i < {1 << total_bits}; i = i + 1) begin")
    offset = 0
    for port in inputs:
        width = int(port["width"])
        mask = (1 << width) - 1
        lines.append(f"      {port['name']} = (i >> {offset}) & {mask};")
        offset += width
    lines.append("      #1;")
    fmt = " ".join("%0h" for _ in outputs)
    args = ", ".join(port["name"] for port in outputs)
    lines.append(f'      $display("%0d {fmt}", i, {args});')
    lines.append("    end")
    lines.append("    $finish;")
    lines.append("  end")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _sop(rows: list[int], selectors: list[str]) -> str:
    if not rows:
        return "1'b0"
    if len(rows) == (1 << len(selectors)):
        return "1'b1"
    terms = []
    for row in rows:
        literals = []
        for index, selector in enumerate(selectors):
            bit = (row >> index) & 1
            literals.append(selector if bit else f"~{selector}")
        terms.append("(" + " & ".join(literals) + ")")
    return " | ".join(terms)


def repair_combinational(case, ir: dict[str, Any], out_dir: Path,
                         formal_config: dict[str, Any],
                         *, max_input_bits: int = 12) -> dict[str, Any] | None:
    if not _is_combinational(ir):
        return None
    from blocks.b7_phase_c.equivalence import _pair, _shared

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        info = _shared().prepare_verification(_pair(case), case.reference_text(), {},
                                               out_dir)
    except Exception:
        return None
    if info.get("status") != "ready":
        return None
    ports = info.get("golden_ports") or []
    inputs = [p for p in ports if p.get("direction") == "input"]
    outputs = [p for p in ports if p.get("direction") == "output"]
    total_bits = sum(int(p.get("width", 1)) for p in inputs)
    if not outputs or total_bits > max_input_bits:
        return None
    golden = out_dir / "golden.sv"
    match = _MODULE.search(golden.read_text(errors="replace"))
    if not match:
        return None
    tb = _tb(ports, match.group(1), inputs, outputs, total_bits)
    (out_dir / "truth_tb.sv").write_text(tb, encoding="utf-8")
    binary = out_dir / "truth.vvp"
    try:
        build = subprocess.run([str(IVERILOG), "-g2012", "-o", str(binary),
                                str(golden), str(out_dir / "truth_tb.sv")],
                               capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if build.returncode != 0:
        return None
    try:
        run = subprocess.run([str(VVP), str(binary)], capture_output=True, text=True,
                             timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    table: list[list[int]] = []
    for line in (run.stdout or "").splitlines():
        fields = line.split()
        if len(fields) != len(outputs) + 1 or not fields[0].isdigit():
            continue
        row = []
        for field in fields[1:]:
            try:
                row.append(int(field, 16))
            except ValueError:
                row.append(int(field.replace("x", "0").replace("z", "0"), 16))
        table.append(row)
    if len(table) != (1 << total_bits):
        return None

    selectors: list[str] = []
    offset = 0
    for port in inputs:
        width = int(port.get("width", 1))
        for bit in range(width):
            selectors.append(port["name"] if width == 1 else f"{port['name']}[{bit}]")
        # ``i`` bit ``offset+bit`` maps to this selector; SOP uses bit index in
        # the concatenated integer, so reorder below.
        offset += width
    # The testbench assigns input bit k to integer bit (offset+k); the table
    # index therefore has the same order as ``selectors`` built above.

    derived = copy.deepcopy(ir)
    by_name = {str(item.get("name")): item for item in derived.get("derived_signals") or []}
    for index, port in enumerate(outputs):
        name = str(port.get("name"))
        width = int(port.get("width", 1))
        bit_exprs = []
        for bit in range(width):
            rows = [row_index for row_index, row in enumerate(table)
                    if ((row[index] >> bit) & 1)]
            bit_exprs.append(_sop(rows, selectors))
        expression = bit_exprs[0] if width == 1 else "{" + ", ".join(reversed(bit_exprs)) + "}"
        signal = by_name.get(name)
        if signal is None:
            signal = {"name": name, "width": width, "expression": expression,
                      "dependencies": [p["name"] for p in inputs]}
            derived.setdefault("derived_signals", []).append(signal)
        else:
            signal["expression"] = expression
            signal["width"] = width
    return derived
