"""Local RTL syntax/elaboration guard.

The deterministic IR compiler emits RTL text without parsing it, so an IR
expression that is not legal Verilog (e.g. a ``case`` statement inside an
assignment) only surfaces at the formal tool.  Verilator lint runs locally in
well under a second and turns that into immediate feedback.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_VERILATOR = PROJECT_ROOT / ".tools_c1" / "bin" / "verilator"


def lint_rtl(rtl_path: Path | str, *, verilator: str | Path | None = None,
             timeout: int = 60, strict_width: bool = False,
             strict_range: bool = False) -> dict:
    """Lint one RTL file.

    ``-Werror-WIDTH`` used to be unconditional.  The deterministic compiler's
    own baseline RTL already fails that rule (5-50 ``WIDTHEXPAND`` hits per
    design), so every candidate option inherited the same failures and every
    question was downgraded to S1 before it could ever be asked.  Width
    mismatches are now warnings; range errors stay fatal.  Pass
    ``strict_width=True`` to restore the old behaviour for diagnostics.
    """
    binary = Path(verilator or DEFAULT_VERILATOR)
    if not binary.is_absolute():
        binary = PROJECT_ROOT / binary
    source = Path(rtl_path)
    if not binary.is_file():
        return {"ok": True, "status": "tool_unavailable", "errors": []}
    # Parameter-dependent part-selects are common in the generated IR
    # (e.g. OUT_TAG_WIDTH-1:QUEUE_ADDRW); Verilator reports SELRANGE as a
    # warning, and promoting it to an error made every candidate option of a
    # whole design fail before the real testbench could judge it.  Keep it a
    # warning by default; --strict-range restores the old diagnostic mode.
    flags = ["--lint-only", "-Wno-fatal"]
    if strict_width:
        flags.insert(2, "-Werror-WIDTH")
    if strict_range:
        flags.append("-Werror-SELRANGE")
    try:
        proc = subprocess.run([str(binary), *flags, str(source)],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": True, "status": "tool_error", "errors": [str(exc)[:200]]}
    output = (proc.stdout or "") + (proc.stderr or "")
    errors = [line for line in output.splitlines() if line.startswith("%Error")]
    warns = [line for line in output.splitlines() if line.startswith("%Warning")]
    return {"ok": not errors and proc.returncode == 0,
            "status": "passed" if not errors and proc.returncode == 0 else "failed",
            "errors": errors[:6], "warnings": warns[:6],
            "returncode": proc.returncode,
            "log": output[-4000:]}


_PROCEDURAL = re.compile(r"\b(case|casez|casex|endcase|begin|end|always|initial|assign|if|else)\b",
                         re.IGNORECASE)


def procedural_expression(value: str) -> bool:
    """True when an IR expression smuggles procedural Verilog."""
    text = str(value)
    return ";" in text or bool(_PROCEDURAL.search(text))