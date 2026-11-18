import json
import re
from pathlib import Path

from .backend import generate_verilog
from .lowering import build_processes
from .parser import parse_ir
from .validator import validate_ir


def _unconsumed(model, rtl: str) -> list[str]:
    """States/transitions that never appear in the emitted RTL.

    A module whose IR is ignored (a hand-written template, a hard-coded
    renderer) still reports ``status: ok``.  This check makes that visible and
    blocks the "IR is decorative" failure mode.
    """
    missing = []
    for state in model.states:
        name = str(getattr(state, "name", ""))
        if name and not re.search(rf"\b{re.escape(name)}\b", rtl):
            missing.append(f"state:{name}")
    for transition in model.transitions:
        if transition.transition_type == "combinational":
            continue
        targets = [action.base_target for action in transition.actions]
        if targets and not any(re.search(rf"\b{re.escape(target)}\b", rtl) for target in targets):
            missing.append(f"transition:{transition.name}")
    return missing


def compile_ir(ir_path, output_path, report_path=None, source_map_path=None):
    """Compile YAML/JSON Petri Net IR to synthesizable Verilog."""
    model = parse_ir(ir_path)
    warnings = validate_ir(model)
    mapping = {}
    rtl = generate_verilog(model, source_map=mapping if source_map_path else None)
    unconsumed = _unconsumed(model, rtl)
    if unconsumed:
        warnings.append("ir_not_consumed: " + ", ".join(unconsumed[:12]))
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rtl, encoding="utf-8")

    if source_map_path:
        import hashlib
        mapping['ir_hash'] = hashlib.sha256(Path(ir_path).read_bytes()).hexdigest()
        mapping['module'] = model.module_name
        destination = Path(source_map_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    processes = build_processes(model)
    report = {
        "status": "ok",
        "input_ir": str(Path(ir_path).resolve()),
        "output_rtl": str(output),
        "module": model.module_name,
        "counts": {
            "ports": len(model.ports),
            "clock_domains": len(model.clock_domains),
            "states": len(model.states),
            "transitions": len(model.transitions),
            "sequential_processes": len(processes),
            "resources": len(model.resources),
            "cdc_crossings": len(model.cdc),
        },
        "warnings": list(warnings),
    }
    if report_path:
        report_output = Path(report_path).resolve()
        report_output.parent.mkdir(parents=True, exist_ok=True)
        report_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    return output, report
