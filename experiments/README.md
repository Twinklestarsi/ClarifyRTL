# version2 对比实验运行说明

## 数据

- clean / fuzzed 数据集：`data/`，详见 [data/README.md](data/README.md)。
- 统一 runner 视图：`runner_cases/`（222 cases，6 个 dataset label，clean/ambig 成对）。
- 每个 case：`prompt.md` + `reference.*` + `testbench.*`；solver 只读 `prompt.md`。

## 系统

| system | 配置 |
|---|---|
| `ours` | A2_ranked_prune，repair=4，rewrite=4，do-no-harm 常开，library=null |
| `ours_no_dag` | A0_no_question（问/答模块关闭），repair=4，rewrite=4 |
| `ours_no_feedback` | A2_ranked_prune，repair=0，rewrite=0（问/答保留） |
| `cppl` | `adapters/run_cppl.py`，CPPL DSL + CIRCT |
| `autochip` | `adapters/run_autochip.py`，上游 iverilog 反馈循环 |
| `rtlcoder` | `adapters/run_rtlcoder.py --mode api_prompt`，RTLCoder 指令模板 + AIC 模型 |

模型：`deepseek-v4.1-flash-1017`、`qwen3.8-27b`，都走 AIC；见 [models.yaml](models.yaml)。
RTLCoder 本地 GGUF 权重也已下载（`_rtlcoder_local/`），但矩阵统一用 API prompt 模式，`result.json` 会标 `prompt_style=true`。

## 运行

```bash
# 全量 ours（2 个模型 × 3 个 ours 变体 × 3 个 family × 2 repeats）
python3 run_ours_matrix.py --families rtllm,verilogeval,tempo \
  --systems ours,ours_no_dag,ours_no_feedback --models deepseek,qwen \
  --repeats 2 --workers 2 --job-workers 6 --tag run1

# 三个工具
python3 run_tools_matrix.py --tools cppl,autochip,rtlcoder \
  --models deepseek,qwen --repeats 2 --workers 4 --tag run1

# 实时汇总
python3 collect_matrix.py
```

两个脚本都支持断点续跑：已有 `result.json` 的 case 会跳过；工具失败的 case 会重试 2 次。
`run_ours_matrix.py` 按 2 个 case 一组交错跑 clean/ambig，保证配对数据同步产生。

## 结果

```text
runs/
  REPORT.md                      实时汇总（每分钟刷新）
  report.json                    机器可读汇总
  tools_matrix/run1/progress.jsonl   每个工具 case 一行
  tools_matrix/run1/<tool>/<model>/<label>/r<repeat>/<case>/result.json
  ours_matrix/run1/<system>/<model>/<family>/r<repeat>/<LABEL>__<case>/<run_id>/result.json
```

`REPORT.md` 的 `avg Q` 是 ours 每个 case 平均实际提问数；工具为 0。

## 已知口径

- 评估统一用 Verilator + 固定 testbench。RTLLM 的 `===========Error===========` 和 VerilogEval 的 `Mismatches: N in M samples` 都已纳入失败判据；VerilogEval TB 的顶层选实例化 DUT 的 `tb`，并把 reference 作为 `RefModule` 一起编译。
- CPPL 的 `result.json` 会覆盖 `common.evaluate_rtl` 的假通过（`eval_override`），以完整 simulation.log 为准。
- AutoChip 不返回 OpenAI usage；汇总里的 token 是从其 `autochip/out/log.txt` 字符数估算的，标为 `estimated_from`。
- `ours` 的 do-no-harm 在代码里是常开的（没有独立开关）；library 通过 `repair_library=null` 关闭。
- 目前跑的是 2 个独立重复（r0/r1）；目标 5 次时，用 `--repeats 5` 重跑同一 tag 即可补 r2–r4。
