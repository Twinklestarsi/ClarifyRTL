# DAC2026：可迁移的 RTL 生成与 Contribution 1 模型

此发布包面向 **Linux x86_64、glibc ≥ 2.28**（如 Ubuntu 20.04/22.04/24.04）。包含完整主流程代码、C1 训练/评估代码、推荐图模型、配套校准文件、冻结 MiniLM 编码器，以及不需要 API 的端到端例子。CPU 即可运行，不需要 CUDA。

## 1. 解压与创建环境

将 `dac2026_c1_runtime.tar.gz` 复制到新服务器，在任意目录执行：

```bash
tar -xzf dac2026_c1_runtime.tar.gz
cd dac2026_c1
conda env create -f environment.yml
conda activate dac2026-c1
python -m pip install -r requirements.txt
```

`environment.yml` 安装 Python 3.9、Verilator 5.x、Icarus Verilog、make 和 C++ 编译器；`requirements.txt` 安装 Python 依赖。无需使用 sudo 或手动安装系统编译工具。已有同名 Conda 环境时，可在创建命令中加 `-n 另一个名字`，并相应激活它。

`requirements.lock.txt` 固定本次干净环境解析出的完整 Python 依赖，已由 `requirements.txt` 自动引用。`conda-linux-64.lock` 另存了本次原生工具的精确版本，用于后续追溯。

这些命令安装依赖时需要联网。模型权重已在包内，运行例子不会下载模型。仅从自然语言生成 IR、使用 `--c1-llm` 或调用 LLM 修复时需要可用的 LLM 服务。

## 2. 检查安装并跑一个完整例子

```bash
python -m deployment.doctor
python -m deployment.smoke_test
```

检查程序会验证 Python 包、编译器、模型/校准 SHA256、冻结编码器文件与版本。离线例子执行：已有 IR → checker1/2 → RTL 编译 → C1 图模型预测 → Verilator 测试平台仿真 → 有界查询与 RTL 回放，不需要 API key。

成功时显示 `ENVIRONMENT CHECK: PASSED` 与 `OFFLINE SMOKE TEST: PASSED`。结果在：

```text
generation/migration_smoke/generated_rtl.v
generation/migration_smoke/pipeline_manifest.json
generation/migration_smoke/smoke_result.json
generation/migration_smoke/contribution1/runs/.../attempts/attempt_0/report.md
```

可选回归检查：

```bash
python -m unittest discover -s tests -t . -q
```

## 3. 运行自己的自然语言规格

在新服务器重新配置 LLM 服务，发布包不包含原服务器的密钥或 `.env`：

```bash
cp api.env.example api.env
# 编辑 api.env，填入服务地址、API key、模型名
source api.env
python run_c1.py /absolute/path/to/spec.md \
  --generation-dir generation/my_design \
  --c1-llm
```

`run_c1.py` 自动使用包内权重、冻结编码器、校准文件、K=8 和 1000ms 查询预算，并启用有界分析。可从任意目录通过完整脚本路径调用；相对输入路径按调用者当前目录解释。

已有 IR 时：

```bash
python run_c1.py /path/to/spec.md --ir /path/to/ir.yaml \
  --generation-dir generation/my_ir --no-sva --no-hw-ir-repair
```

该命令的 IR 路径不会触发生成调用。主流程选项仍由 `main.py` 提供：

```bash
python main.py --help
```

直接调用 `main.py` 加载本模型的等效参数为：

```bash
python main.py /path/to/spec.md \
  --c1-checkpoint models/contribution1/best.npz \
  --c1-encoder-path models/all-MiniLM-L6-v2 \
  --c1-calibrator models/contribution1/calibrator.json \
  --c1-horizon 8 --c1-solver-timeout-ms 1000 --c1-bounded --c1-llm
```

## 4. 文件布局与模型范围

```text
main.py / run_c1.py              # 原始入口 / 已配好模型的入口
environment.yml                 # Conda 和原生 RTL 工具
requirements.txt                # 完整 CPU Python 依赖
models/manifest.json             # 位置无关路径、版本和文件校验
models/contribution1/best.npz    # 推荐 C1 图模型，保留原始训练参数
models/contribution1/calibrator.json
models/all-MiniLM-L6-v2/         # 真实文件，无外部缓存软链接
models/contribution1/training_provenance/
examples/counter/               # 可离线运行的规格、IR、测试平台
deployment/                    # 环境检查、离线验证、打包工具
```

推荐模型具有 1,130,888 个可训练参数，使用冻结的 384 维 MiniLM 编码器，训练快照为 G1、K=8。它是 20 个 RTLLM 设计、400 个受控状态上的机器校验实验模型，未完成独立人工审查。本批监督覆盖 `reset_value` 和 `state_update`；其他决策类型不输出学习预测。

测试只有两个原始设计。状态分类 Macro-F1 约 0.733，影响定位平均 F1 约 0.426，修改成本排序尚不可靠。模型帮助分析设计选择，不构成 RTL 正确性的保证。历史训练报告中的原服务器路径保留作来源记录，不是新服务器运行依赖；实际运行路径全部由当前发布目录解析。

请使用完整 `requirements.txt`：特别是 `torch==2.6.0+cpu`、Sentence Transformers 与 Transformers 的版本参与编码器一致性校验。即使新服务器有 GPU，也先使用本 CPU 环境；直接换 CUDA 构建或升级相关库会导致版本校验失败。

## 5. 可选：迁移训练数据并重新训练

只运行生成与预测无需训练数据包。需要重新训练/审查时，在与运行包相同的父目录解压：

```bash
tar -xzf dac2026_c1_training_data.tar.gz
cd dac2026_c1
python -m experiments.contribution1.run_rtllm20 --stage train
```

数据包包含平衡后的 400 个样本、标签与证据、真实耗时测量来源，以及本地 RTLLM 原始文本/RTL/TB。配置中的编码器路径是包内相对路径。重新训练使用新配置和新输出目录，不宣称跨服务器精确恢复旧训练过程；计时标签描述原记录的机器和固定工作流，换服务器后应重新测量才能代表新机器的耗时。

请使用默认的 `rtllm20_balanced_train.yaml` 配置。数据包中的旧 `datasets/contribution1_rtllm20` 目录仅保留成本测量来源，完整可训练数据位于 `datasets/contribution1_rtllm20_balanced`。历史 GPS 对比演示源码也予以保留；它另外需要 GPS 仓库，可通过 `GPS_SOURCE` 指定，与主流程和 C1 模型运行无关。

若已有训练结果需要保留，先修改 `experiments/contribution1/configs/rtllm20_balanced_train.yaml` 的 `run_dir`。旧语义模型和旧虚拟环境不打包。历史来源/证据中的绝对路径可用于追溯，训练加载依赖的是相对 manifest、输入图和标签，不需要原服务器挂载目录。

## 6. 包校验与重新打包

传输前后可以对比外层 `SHA256SUMS`；进入解压目录后可执行 `sha256sum -c SHA256SUMS` 检查包内原始文件。API 配置和新生成结果不是校验清单的一部分。

已安装完整环境的项目中重新组装：

```bash
python -m deployment.build_release --with-training-data
```

这会生成 `dist/dac2026_c1_runtime.tar.gz`、可选训练数据包和校验文件。不会打包虚拟环境、API 配置、缓存软链接或历史编译二进制。
