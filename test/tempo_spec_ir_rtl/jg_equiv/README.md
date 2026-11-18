# JasperGold 等价性检查（Tempo Spec → IR → RTL）

本目录把 `run_tempo.py` 产出的十例 RTL 与 Tempo 参考 RTL 做 Cadence JasperGold
**SEC（Sequential Equivalence Checking）**，用于回答两个问题：

1. 生成的 RTL 是否是合法的 SystemVerilog，能否被工业级工具 elaborate；
2. 在相同输入序列、相同同步复位下，生成的 RTL 与参考 RTL 的每个输出是否逐拍等价。

## 文件

| 文件 | 作用 |
| --- | --- |
| `normalize_rtl.py` | 只改写参数默认值中的非合法写法（`clog2(...)`→`$clog2(...)`、`max(a,b)`→三元表达式、空/裸字符串参数），并给实现模块改名；不改变任何逻辑、状态或端口 |
| `run_equiv.py` | 驱动脚本：逐例做「原样 analyze 检查 → 规范化 → SEC 证明 → 汇总报告」，产出 `reports/summary.{json,md}` |
| `work/<case>/` | 每例的工作目录：`spec.sv`（参考 RTL）、`impl_raw.v`（原始生成 RTL）、`impl.v`（规范化后）、`sec.tcl`、`run.log`、`result.json` |
| `reports/` | 汇总结果 |

## 用法

```bash
cd /nas2nfs/homes/2531930/test/tempo_spec_ir_rtl
# 先跑主流程（十例）
.venv_c1/bin/python run_tempo.py --all --env-file api.env --run-id tempo10_final
# 再做 JasperGold 等价性检查
.venv_c1/bin/python jg_equiv/run_equiv.py --run-dir runs/tempo10_final
```

单例、限时：

```bash
.venv_c1/bin/python jg_equiv/run_equiv.py --run-dir runs/tempo10_final --cases dp_ram --prove-limit 5m
```

带方法学对照（把参考 RTL 与自身比较，确认 harness 本身能证出 `equivalent`）：

```bash
.venv_c1/bin/python jg_equiv/run_equiv.py --run-dir runs/tempo10_final --selfcheck
```

## 等价性问题的形式化定义

每例的 `sec.tcl` 等价于：

```tcl
check_sec -setup -spec_top <ref_module> -spec_analyze_opts {-sv spec.sv} \
                   -imp_top  <gen_module> -imp_analyze_opts  {-sv impl.v} \
                   -auto_map_reset_x_values
clock clk
reset -expression reset -non_resettable_regs 0
check_sec -interface
check_sec -prove -strategy basic -time_limit <limit>
```

- **spec**：Tempo 参考 RTL（`runs/<run>/<case>/reference/*.sv`）；
- **imp**：流程生成的 RTL（`runs/<run>/<case>/generation/generated_rtl.v`）；
- 两边共享 `clk`；`reset` 用 `reset -expression` 建模为「复位期间为 1，证明阶段恒为 0」
  的同步复位，与两份 RTL 中 `always @(posedge clk) if (reset) ... else ...` 的语义一致；
- `-non_resettable_regs 0` 把未被复位覆盖的寄存器（含存储体）初始值在两侧都固定为 0，
  这样初始状态完全确定，不会因为两侧 X 不同而产生伪反例（`--init-nonresettable none` 可关闭）；
- 主输入按名字自动连接，主输出（boundary outputs）两两比较；
- `-auto_map_reset_x_values` 把两侧都未被复位的寄存器初始值成对绑定。

因此判定为 `equivalent` 意味着：**在任意输入序列、同一复位序列下，所有输出逐拍相同**；
`not_equivalent` 表示找到了反例（`sec_counts.cex` 给出数量）。

## 结果状态

| status | 含义 |
| --- | --- |
| `equivalent` | 所有输出证明等价 |
| `not_equivalent` | 存在反例，两侧 RTL 行为不同 |
| `undetermined` | 命题未证明也未证伪（时间/复杂度） |
| `parse_error` | 生成 RTL 有语法/类型错误，无法 analyze |
| `impl_elaboration_error` | 语法通过但 elaborate 失败（如数组下标越界、参数非法） |
| `spec_elaboration_error` | 参考 RTL 无法 elaborate（本数据集未出现） |
| `timeout` / `jg_error` | 超时或工具错误 |

`reports/summary.json` 中每例还记录了首个错误、对应的生成 RTL 行号与源码文本
（`defect_line`），便于直接定位生成器缺陷。

## 注意

- 生成 RTL 的原始文件**不会被修改**；规范化结果只写进 `work/<case>/impl.v`。
- JasperGold 启动时会通过 HTTP 代理连自己的 session server，若环境里有
  `http_proxy/all_proxy` 会导致 `Failed to start session server`，驱动脚本会自动清掉这些变量。
- 生成的 RTL 在默认参数下若本身非法（如 `valid_pipe[READ_LATENCY-2:0]` 在
  `READ_LATENCY=1` 时下标为 -1），SEC 只能报告 `impl_elaboration_error`，这本身是流程缺陷。