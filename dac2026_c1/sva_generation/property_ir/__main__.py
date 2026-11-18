"""Command-line entry point for Temporal Property IR extraction."""

import argparse
import os
import sys
from pathlib import Path

from . import (PropertyIRCheckerError, check_property_ir,
               generate_property_ir, generate_spec_property_ir)


def main():
    parser = argparse.ArgumentParser(
        description=("Generate Temporal Property IR from a SPEC; HW-IR is "
                     "used only for valid symbol binding"))
    parser.add_argument("hw_ir", help="HW-IR YAML/JSON that passed checker2")
    parser.add_argument(
        "--spec",
        help="Hardware specification; omit it to use legacy HW-IR compatibility mode")
    parser.add_argument("--output", help="Property IR output path")
    parser.add_argument("--report", help="Property IR checker report path")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL"))
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"))
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=8192)
    args = parser.parse_args()
    hw_ir = Path(args.hw_ir).resolve()
    output = Path(args.output).resolve() if args.output else hw_ir.with_name(
        "temporal_property_ir.yaml")
    report = Path(args.report).resolve() if args.report else output.with_name(
        "property_ir_checker_report.json")
    try:
        if args.spec:
            generate_spec_property_ir(
                Path(args.spec).resolve(), hw_ir, output,
                api_key=args.api_key, base_url=args.base_url, model=args.model,
                temperature=args.temperature, max_tokens=args.max_tokens)
        else:
            generate_property_ir(hw_ir, output)
        result = check_property_ir(hw_ir, output, report)
    except (ValueError, RuntimeError, PropertyIRCheckerError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Property IR: {output}")
    print(f"Checker: {report} ({result['status']}, {result['property_count']} properties)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
