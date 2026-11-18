# Spec 歧义生成（version2 对比实验）

对 `experiments/data/` 下的 clean spec 做受控模糊处理：不是把正确规则改错，而是删除/弱化一个必要条件，使原始行为仍是合法解释，同时出现另一种可观察的合理实现。每条歧义点都能由 DAG 的一个澄清问题问出来，答案来自原始 spec / reference RTL。

## 目录

```text
experiments/fuzz/
  fuzz_specs.py       生成 + 审计 + 重试
  build_ambiguous.py  把通过审计的结果组装成 *_ambig 数据集
  summarize.py        汇总通过率、site 数、类别分布、token
  out/<dataset>/<case>/
    result.json           最终结果（status/sites/edited_spec/audit/usage）
    edited_spec.md        模糊版 spec
    attempt_N_generate.json
    attempt_N_audit.json
  logs/run_*.jsonl
```

## 规则

- 目标类别是 DAG 的 C1–C8：并发优先级、时序、边界、复位、握手、状态保持、存储冲突、位宽/参数。
- **RTLLM / VerilogEval**：每条 1–8 个歧义点，由模型按复杂度决定；审计不通过的 site 会被丢弃（salvage），至少保留 1 个。
- **Tempo**：每条最终 **8 个**歧义点。模型先提 10–12 个候选，审计后按置信度保留 8 个；不足 8 个则整条重试。
- 不修改 module 名、端口名/位宽/方向、parameter 名/默认值、module header。
- 不允许直接写错规则；不允许加入 “ambiguous / clarify / unspecified” 之类的元语言。
- 每条 site 必须有：原始片段、替换片段、原意、另一种解释、澄清问题、答案、可观察差异。
- 生成后由第二次 LLM 调用审计；审计不通过会带反馈重试，最多 3 次。

## 运行

```bash
cd /nas2nfs/homes/2531930/ONE-TURN
PY=python3

# 单条调试 / 看 prompt
$PY version2/experiments/fuzz/fuzz_specs.py --dataset rtllm --cases accu --dry-run

# 小批
$PY version2/experiments/fuzz/fuzz_specs.py --dataset tempo --cases dp_ram,coalescer --workers 2

# 全量（已通过的 case 会跳过；加 --force 重跑）
$PY version2/experiments/fuzz/fuzz_specs.py --dataset all --workers 4 --attempts 3

# 汇总
$PY version2/experiments/fuzz/summarize.py

# 组装 *_ambig 数据集
$PY version2/experiments/fuzz/build_ambiguous.py --force
```

## 输出数据集

```text
experiments/data/group1/rtllm_ambig/
experiments/data/group1/verilogeval_ambig/
experiments/data/group2/tempo_ambig/
```

每个 case 保留 clean 的 `reference.*` / `testbench.*`，只替换 `prompt.md`；完整的 site 标注、原始 spec、审计结果保存在 `annotations/<short>.json`，**不在 case 目录里，不能进入 solver 上下文**。

## 已知限制

- 审计是 LLM 判断，不是形式化证明；少数 site 可能过松或过严，需要人工抽查 `REVIEW.md`。
- 如果 reference RTL 与原始 spec 本身冲突（例如某些 RTLLM 设计），该 case 可能找不到合法歧义点，会被标记 failed；不要强行注入。
- 简单组合逻辑（如纯 Karnaugh 表）可能天然没有可注入的行为歧义，这类 case 会失败，最终按 paired 子集处理。
- 生成使用 `deepseek-v4.1-flash-1017`（`DAG_BASE_URL` / `DAG_API_KEY`）；不要把 key 写入日志或结果。