# Version 1.0 — runnable experiment snapshot

This directory is a self-contained copy of the Spec -> HW-IR -> RTL experiment
source and the data needed by the current Tempo26 / RTLLM / VerilogEval runs.
EDA tools and Python virtualenvs are intentionally not copied.

## Layout

```text
dac2026_c1/                 Compiler, IRgeneration, assumption_modeling, iteration,
                            sva_generation, and experiments/ir_question_dag
DAG/experiment/llm_generation_errors/
                            the 38-case dataset (Tempo26/RTLLM/VerilogEval)
tempo26/vortex/             Tempo26 specs/reference/testbenches
test/tempo_spec_ir_rtl/     test_monolithic generation frontend source
```

Excluded: `runs/`, `_generation_cache/`, `__pycache__/`, `.venv*`, `models/`,
`datasets/`, `.tools_c1/`, and API secrets.

## Environment

Use an existing Python environment (for example the original
`dac2026_c1/.venv_c1/bin/python`) or install `dac2026_c1/requirements.txt`.
Verilator is configured in `dac2026_c1/experiments/ir_question_dag/config/default.yaml`
at the external EDA path; JasperGold remains an external SSH resource configured
under `formal`.

Provider credentials are not copied. Create an `api.env` with:

```bash
export OPENAI_API_KEY='...'
export OPENAI_BASE_URL='https://.../v1'
export OPENAI_MODEL='...'
source api.env
```

Environment variables take precedence over the repository's old `.env` paths.

## Run

```bash
cd /home/2531930/nas/2531930-2/ONE-TURN/version_1.0/dac2026_c1/experiments/ir_question_dag
PY=/nas2nfs/homes/2531930/ONE-TURN/dac2026_c1/.venv_c1/bin/python

# Tempo26, saved-testbench Verilator validation
$PY src/run_case.py --all --dataset Tempo26 --arm A2_ranked_prune --workers 4

# RTLLM / VerilogEval, original JasperGold validation
$PY src/run_case.py --all --dataset RTLLM --arm A2_ranked_prune --workers 8
$PY src/run_case.py --all --dataset VerilogEval --arm A2_ranked_prune --workers 8
```

Results are written under
`dac2026_c1/experiments/ir_question_dag/runs/`.
# ClarifyRTL
