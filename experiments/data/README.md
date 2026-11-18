# version2 对比实验数据

- 建立时间：2026-11-15
- 生成脚本：[prepare_clean.py](prepare_clean.py)、[../fuzz/fuzz_specs.py](../fuzz/fuzz_specs.py)、[../fuzz/build_ambiguous.py](../fuzz/build_ambiguous.py)
- 原则：**只复制，不移动、不修改原始语料**；原始目录仍可继续被现有 DAG 流程使用。

## 1. 目录

```text
experiments/data/
  group1/
    rtllm/                48 clean
    rtllm_ambig/          46 clean + 46 fuzzed 的 paired 子集
    verilogeval/          69 clean
    verilogeval_ambig/    55 clean + 55 fuzzed 的 paired 子集
    pairs.json            group1 的 paired 清单
  group2/
    tempo/                10 clean
    tempo_ambig/          10 clean + 10 fuzzed
    pairs.json            group2 的 paired 清单
  manifest.json           clean 总清单
  summary.json            clean/ambig/paired 的机器可读汇总
```

clean 与 ambig 的 case 目录只放 solver 可见/评测必需的文件：

```text
<dataset>/cases/<short>/
  prompt.md        送给模型的可见 spec
  reference.v|sv   参考 RTL（仅 oracle / 评测用，不能泄露给 solver）
  testbench.v|sv   固定 testbench
  meta.json        case_id、top_module、源路径、sha256（clean 才有）
  interface.txt   仅 VerilogEval，原始 ifc 文件

<dataset>_ambig/cases/<short>/
  prompt.md        模糊后的 spec
  reference.*      与 clean 相同
  testbench.*      与 clean 相同
  interface.txt    仅 VerilogEval

<dataset>_ambig/annotations/<short>.json
  sites、original_spec、审计结果、usage；作者/评测器材料
<dataset>_ambig/annotations/<short>.meta.json
  case 元信息 + n_sites + categories
```

作者侧标注放在 `annotations/`，**不在 case 目录里**，避免 solver 或工具误读。

## 2. 选择规则

| 数据集 | clean | 模糊版 | 说明 |
|---|---:|---:|---|
| RTLLM | 48 | 46（54 个歧义点） | 全部 48 个设计；2 个无法安全注入（`adder_32bit`、`fixed_point_adder`） |
| VerilogEval | 69 | 55（72 个歧义点） | 23 个现有失败例 + 其余 133 个中 seed `20261115` 随机抽 46 个 |
| Tempo | 10 | 10（80 个歧义点） | 每个设计最终强制 **8 个**歧义点 |

VerilogEval 的 46 个随机案例见 `group1/verilogeval/manifest.json` 的 `selection.random_46`。

## 3. 模糊处理规则

- 目标类别是 DAG 的 C1–C8：并发优先级、时序、边界、复位、握手、状态保持、存储冲突、位宽/参数。
- 不是把正确规则改错，而是删除/弱化一个必要条件，使**原始行为仍是合法解释**，同时出现另一种可观察的合理实现。
- 每个歧义点必须带：原始片段、替换片段、原意、另一种解释、澄清问题、答案、可观察差异。
- 不修改 module 名、端口名/位宽/方向、parameter 名/默认值、module header。
- 生成后由第二次 LLM 调用审计；不通过会带反馈重试。RTLLM/VE 的审计失败点会被丢弃（至少保留 1 个）；Tempo 先提 10–12 个候选，审计后按置信度保留 8 个。
- 生成模型：`deepseek-v4.1-flash-1017`（AIC）。

## 4. 通过率与失败案例

| 数据集 | 尝试 | 通过 | 失败 | 通过率 | 歧义点 |
|---|---:|---:|---:|---:|---:|
| RTLLM | 48 | 46 | 2 | 95.8% | 54 |
| VerilogEval | 69 | 55 | 14 | 79.7% | 72 |
| Tempo | 10 | 10 | 0 | 100.0% | 80 |
| **合计** | **127** | **111** | **16** | **87.4%** | **206** |

失败的 16 例（不要强行补）：

- RTLLM：`adder_32bit`（生成器改了端口声明，被接口校验拒绝）、`fixed_point_adder`（审计不通过）
- VerilogEval：`Prob020`、`Prob093`、`Prob098`、`Prob099`、`Prob100`、`Prob101`、`Prob113`、`Prob116`、`Prob125`、`Prob131`、`Prob138`、`Prob139`、`Prob143`、`Prob145`

失败原因多数是纯组合真值表/常量输出，天然没有可询问的行为歧义，或 reference 与 spec 本身冲突。paired 实验应使用 `pairs.json`，不要在 clean 里保留没有对应 ambig 的 case。

## 5. 人工抽查与 QC

- `../fuzz/out/QC.md`：206 个点中 6 个被标记（5 个 replacement 偏长、1 个 confidence 0.6）。标记不代表无效，但建议优先看。
- `../fuzz/out/QC.md` 与 `annotations/<short>.json` 是作者侧材料；solver 只读 `prompt.md`。
- 重点抽查：Tempo 每个 case 的 8 个点是否互不重叠、是否覆盖不同类别；VE 的 `Prob013`（NOR→logic gate）、`Prob031`（D flip-flop→flip-flop）是强歧义，适合做 smoke test。

## 6. 使用

```bash
# 单个 case
ls experiments/data/group2/tempo_ambig/cases/dp_ram

# paired 清单
python3 -c "import json;print(json.load(open('experiments/data/group1/pairs.json'))['counts'])"
python3 -c "import json;print(json.load(open('experiments/data/group2/pairs.json'))['counts'])"
```

solver 只读 `prompt.md`；`reference.*`、`testbench.*`、`meta.json`、`annotations/` 不得进入 solver 上下文。

## 7. 校验

```bash
python3 version2/experiments/data/prepare_clean.py          # clean 已建好会拒绝覆盖
python3 version2/experiments/fuzz/fuzz_specs.py --dataset all --workers 4 --attempts 3
python3 version2/experiments/fuzz/build_ambiguous.py --force
python3 version2/experiments/data/build_pairs.py
python3 version2/experiments/fuzz/summarize.py
python3 version2/experiments/fuzz/qc.py
```