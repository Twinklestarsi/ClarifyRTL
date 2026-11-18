"""Insert an SVA include fragment into generated single-module RTL."""

import re
from pathlib import Path


class SVAInstrumentationError(RuntimeError):
    pass


def instrument_rtl_with_sva(rtl_path, sva_path, output_path):
    """Write a simulation-only RTL copy containing SVA before ``endmodule``."""
    rtl = Path(rtl_path).resolve()
    sva = Path(sva_path).resolve()
    output = Path(output_path).resolve()
    rtl_text = rtl.read_text(encoding="utf-8")
    sva_text = sva.read_text(encoding="utf-8")
    matches = list(re.finditer(r"(?m)^\s*endmodule\b", rtl_text))
    if not matches:
        raise SVAInstrumentationError(
            f"cannot instrument {rtl}: no endmodule found")
    insertion = matches[-1].start()
    instrumented = (
        rtl_text[:insertion].rstrip() +
        "\n\n  // ---- Temporal properties (simulation only) ----\n" +
        sva_text.rstrip() + "\n\n" +
        rtl_text[insertion:])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(instrumented, encoding="utf-8")
    return output
