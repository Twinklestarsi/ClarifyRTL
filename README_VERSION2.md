# version2 — 当前运行版的独立副本

复制自 `/nas2nfs/homes/2531930/ONE-TURN/version_1.0`（2026-11-15），只做复制，未修改代码内容。
`__pycache__` / `*.pyc` / 运行产物未包含。

## 内容

```text
dac2026_c1/          编译器（Compiler/）、IR 生成（IRgeneration/）、实验流水线
                     （experiments/ir_question_dag）
test/tempo_spec_ir_rtl/   单次生成前端（含它自己的 Compiler 副本，与主编译器同步）
tempo26/vortex/      Tempo26 规格 / 参考 RTL / testbench
DAG/experiment/llm_generation_errors/   38 例数据集（Tempo26 / RTLLM / VerilogEval）
logs/                历史诊断日志
run_configs/         最近所有运行配置（config_v2 … config_v14）
config_version2.yaml 已把路径改到本目录、可直接用的配置
```

## 运行

```bash
PY=/nas2nfs/homes/2531930/ONE-TURN/dac2026_c1/.venv_c1/bin/python   # 外部 venv

cd /home/2531930/nas/2531930-2/ONE-TURN/version3/dac2026_c1/experiments/ir_question_dag
$PY src/run_case.py --all --dataset Tempo26 --arm A2_ranked_prune --workers 5 \
    --config /home/2531930/nas/2531930-2/ONE-TURN/version3/config_version2.yaml
```

结果写到 `version2/runs/<case>/<timestamp>/`。

## 外部依赖（未复制）

- Python venv：`/nas2nfs/homes/2531930/ONE-TURN/dac2026_c1/.venv_c1`
- Verilator：`/home/2531930/nas/2531930-2/eda_tools/conda/bin/verilator`
- JasperGold：SSH 资源，配置在 `config_version2.yaml` 的 `formal` 段
- API 凭据：`/nas2nfs/homes/2531930/ONE-TURN/.env`（DAG_* 前缀）与 `DAG/.env`

## 与 version_1.0 的差别

只包含代码与数据，不含任何 `runs/` 结果；`config_version2.yaml` 的路径指向本目录，
其余配置与 `tempo_v1_0_nolib/config_v2.yaml` 一致（library/truth_repair 关闭、
`initial_rewrite_attempts: 5`）。
