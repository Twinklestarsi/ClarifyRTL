import argparse
import sys

from .checker import check_ir
from .error import Checker1Error


def main():
    parser = argparse.ArgumentParser(
        description="Auto-repair deterministic HW-PN IR format issues")
    parser.add_argument("ir", help="YAML/JSON IR file")
    parser.add_argument("--report", help="JSON report path")
    args = parser.parse_args()
    try:
        report = check_ir(args.ir, args.report)
    except Checker1Error as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"checker1 {report['status']}: {report['input_ir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
