#!/usr/bin/env python3
"""Run main.py with the packaged C1 checkpoint, encoder, calibration and K."""
import os
import subprocess
import sys
from deployment.assets import PROJECT_ROOT,pipeline_arguments


def main():
    os.environ.setdefault('PYTHONNOUSERSITE','1')
    os.environ.setdefault('OMP_NUM_THREADS','2')
    os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
    os.environ.setdefault('MKL_NUM_THREADS','1')
    os.environ.setdefault('TOKENIZERS_PARALLELISM','false')
    try:arguments=pipeline_arguments(sys.argv[1:])
    except (ValueError,FileNotFoundError) as exc:
        print(str(exc),file=sys.stderr);return 2
    return subprocess.run([sys.executable,str(PROJECT_ROOT/'main.py'),*arguments]).returncode


if __name__=='__main__':raise SystemExit(main())
