import argparse
import sys

from .checker import check_ir
from .error import Checker2Error


def main():
    parser = argparse.ArgumentParser(
        description="Normalize and check HW-PN IR hardware semantics")
    parser.add_argument("ir")
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        report = check_ir(args.ir, args.report)
    except Checker2Error as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"checker2 {report['status']}: {report['input_ir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
