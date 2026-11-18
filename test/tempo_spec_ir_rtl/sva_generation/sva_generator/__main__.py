"""Command-line entry point for deterministic SVA generation."""

import argparse
import sys
from pathlib import Path

from sva_generation.property_ir import PropertyIRCheckerError, check_property_ir

from . import SVALoweringError, generate_sva


def main():
    parser = argparse.ArgumentParser(
        description="Deterministically lower Temporal Property IR to SVA")
    parser.add_argument("hw_ir", help="Hardware-aware Petri Net IR")
    parser.add_argument("property_ir", help="Temporal Property IR")
    parser.add_argument("--output", help="SVA output path")
    parser.add_argument("--report", help="SVA generation report path")
    args = parser.parse_args()
    hw_ir = Path(args.hw_ir).resolve()
    property_ir = Path(args.property_ir).resolve()
    output = (Path(args.output).resolve() if args.output else
              property_ir.with_name("generated_properties.sva"))
    report = (Path(args.report).resolve() if args.report else
              output.with_name("sva_generation_report.json"))
    try:
        check_property_ir(hw_ir, property_ir)
        _, report, result = generate_sva(
            hw_ir, property_ir, output, report)
    except (ValueError, RuntimeError, PropertyIRCheckerError,
            SVALoweringError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"SVA: {output}")
    print(f"Report: {report} ({result['assertion_count']} assertions, "
          f"{result['cover_count']} covers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
