#!/usr/bin/env python3
"""Unified command-line entry for the integrated Spec-to-RTL/HLS flow."""

import sys

from iteration.pipeline import main


if __name__ == "__main__":
    sys.exit(main())
