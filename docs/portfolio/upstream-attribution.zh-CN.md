# 上游归属

[English](upstream-attribution.md) | 简体中文

本项目复制、改写、拿来对比或运行了哪些上游内容。按 2026-11-10 的 main 核对。本仓库以 MIT 许可证发布（[LICENSE](../../LICENSE)）；下面的第三方材料保留各自的许可证。

## 复制或改写进本仓库的内容

| 上游 | 版本 | 许可证 | 本仓库中有什么 | 位置 |
|---|---|---|---|---|
| DeepSeek V4.1 Flash tokenizer（[Hugging Face](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash)） | 提交 `dba1be0a`（2026-09-10） | MIT；上游许可证随文件一起放在旁边 | `tokenizer.json`，用于在工具视图发送给模型之前计算 token 数；sha256 不一致的文件会被拒绝加载 | [opspilot/tools/tokenizer/](../../opspilot/tools/tokenizer/)（`PROVENANCE`、`LICENSE`） |
| [HolmesGPT](https://github.com/HolmesGPT/holmesgpt) | 提交 `a0009195` | Apache-2.0；该提交的上游 `LICENSE` 放在 [third_party/holmesgpt/](../../third_party/holmesgpt/)（`LICENSE`、`PROVENANCE`；sha256 与 `scripts/holmes_baseline/run.py` 记录的一致）；该提交没有上游 `NOTICE` 文件 | 原文照搬：`tests/llm/utils/classifiers.py` 中的严格正确性分类器提示词，F1 judge 对两个系统都用它；`prometheus.py` 中的 `topk` 建议文本；工具名 `get_metric_names` / `get_label_values` 沿用其 Prometheus 工具集。改写：系统提示词中的调查方法段落，由 `generic_ask.jinja2` 改写；`otel-demo-ext` profile 系统提示词中的 Kubernetes 调查段落（`replay-candidate-k8s`），由 `generic_ask.jinja2` :56/:58/:69/:73-78 改写 | [opspilot/evaluation/binary_judge.py](../../opspilot/evaluation/binary_judge.py)、[opspilot/tools/otel_demo.py](../../opspilot/tools/otel_demo.py)、[opspilot/instructions/discipline.py](../../opspilot/instructions/discipline.py) |

HolmesGPT 也是主要的设计参考。worker 的轮询/认领/运行结构、循环的步数上限、两种上下文管理机制、大结果摘要和 Alertmanager 告警模型都参照 HolmesGPT，并在代码注释中注明（例如 `opspilot/worker_main.py`、`opspilot/investigation/limits.py`、`opspilot/investigation/context.py`、`opspilot/alertmanager.py`）。每个功能步骤与 HolmesGPT `a0009195` 的对应关系（复用、配置、已确认的上游缺陷或必要扩展）见 [docs/testing/f14-upstream-mapping.md](../testing/f14-upstream-mapping.md)；在其上复现的上游 issue 见 [docs/testing/f14-upstream-issues.md](../testing/f14-upstream-issues.md)；如何重新核对钉住的版本见 [docs/testing/f14-upstream-update-strategy.md](../testing/f14-upstream-update-strategy.md)。

## 对比对象

- **未修改的 HolmesGPT `a0009195`**，用从上游源码构建的镜像、通过它自己的 CLI 运行，作为 F1 考试中的同条件基线（[结果](../results/f1-same-condition-comparison-2026-11.md)（英文）、[基线证据](../evidence/f14-holmes-baseline/)）。镜像在本地构建，不再分发。
- OpenSRE、Stratus、kagent、K8sGPT 和 Pi 只作为设计参考研究过；仓库中没有它们的代码（[来源比较](../research/investigation-source-comparison-2026-09-07.md)、[次级比较](../research/upstream-secondary-benchmarks-2026-09-06.md)、[运行时选型](../research/runtime-selection-2026-09-07.md)）。

## 运行但不再分发

lab 和产品按钉住的版本使用以下组件；仓库引用它们，但不包含它们的代码。

| 组件 | 钉住的版本 | 钉在哪里 |
|---|---|---|
| OpenTelemetry Demo Helm chart（appVersion 2.0.2） | `0.37.8` | `scripts/kind_lab.py` |
| kube-state-metrics chart、Alertmanager chart | `8.6.0`、`1.24.0` | `scripts/kind_lab.py` |
| nginx（lab 中的只读 Prometheus 代理） | `1.28.0-alpine` | `scripts/kind_lab/prometheus-readonly-proxy.yaml` |
| Docker Engine、kind、kubectl、helm（Linux lab 安装） | `29.2.1`、`v0.33.0`、`v1.37.1`、`v4.3.0` | `scripts/kind_lab/install_linux.sh`；[f9-clean-install 证据](../evidence/f9-clean-install-2026-11-09/README.md) |
| PostgreSQL 镜像 | `postgres:17.9`，按 digest | `deploy/compose/compose.yaml` |
| Python 基础镜像、uv | `python:3.12.13-slim`、`uv:0.10.8`，按 digest | `Dockerfile` |
| Python 库（FastAPI、Jinja2、Uvicorn、Pydantic、psycopg、psycopg-pool、SQLAlchemy、Alembic、OpenAI client、OpenTelemetry SDK、LangSmith、tokenizers、httpx2） | 精确版本 | `pyproject.toml`、`uv.lock` |

## 外部服务

- **DeepSeek API**（`deepseek-flash`）：调查用的模型。
- **LangSmith**：lab 运行的 trace 与实验存储（[ADR-0006](../adr/0006-trace-evidence-and-backlog.md)）；产品中默认关闭。
- **z.ai `glm-5.3-flashx` 和 OpenAI `gpt-5.6-luna`**：F1 考试中两个未校准的二元 judge。
