# Upstream attribution

What this project copies, adapts, compares against or runs. Checked against main as of 2026-11-10. This repository does not include a license file of its own.

## Copied or adapted into this repository

| Upstream | Version | License | What is in this repository | Where |
|---|---|---|---|---|
| DeepSeek V4.1 Flash tokenizer ([Hugging Face](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash)) | commit `dba1be0a` (2026-09-10) | MIT; the upstream license is vendored next to the file | `tokenizer.json`, used to count tokens of tool views before they are sent to the model; loading refuses a file whose sha256 differs | [opspilot/tools/tokenizer/](../../opspilot/tools/tokenizer/) (`PROVENANCE`, `LICENSE`) |
| [HolmesGPT](https://github.com/HolmesGPT/holmesgpt) | commit `a0009195` | Apache-2.0 (upstream `LICENSE` at that commit; its sha256 is recorded by `scripts/holmes_baseline/run.py`; no upstream `NOTICE` file at that commit) | Verbatim: the strict correctness classifier prompt from `tests/llm/utils/classifiers.py`, used by the F1 judges for both systems; the `topk` suggestion text from `prometheus.py`; the tool names `get_metric_names` / `get_label_values` follow its Prometheus toolset. Adapted: the investigation-method paragraph of the system prompt, rewritten from `generic_ask.jinja2` | [opspilot/evaluation/binary_judge.py](../../opspilot/evaluation/binary_judge.py), [opspilot/tools/otel_demo.py](../../opspilot/tools/otel_demo.py), [opspilot/instructions/discipline.py](../../opspilot/instructions/discipline.py) |

HolmesGPT is also the main design reference. The worker's poll/claim/run shape, the loop's step limit, the two context-management mechanisms, the large-result summary and the Alertmanager alert model follow HolmesGPT and say so in the code comments (for example `opspilot/worker_main.py`, `opspilot/investigation/limits.py`, `opspilot/investigation/context.py`, `opspilot/alertmanager.py`). Each feature step is mapped to HolmesGPT `a0009195` as reuse, configuration, confirmed upstream defect or required extension in [docs/testing/f14-upstream-mapping.md](../testing/f14-upstream-mapping.md); the upstream issues reproduced against it are in [docs/testing/f14-upstream-issues.md](../testing/f14-upstream-issues.md), and how the pin is re-checked is in [docs/testing/f14-upstream-update-strategy.md](../testing/f14-upstream-update-strategy.md).

## Compared against

- **HolmesGPT `a0009195`, unmodified**, run through its own CLI from an image built from upstream source, as the same-condition baseline in the F1 exam ([results](../results/f1-same-condition-comparison-2026-11.md), [baseline evidence](../evidence/f14-holmes-baseline/)). The image is built locally and not redistributed.
- OpenSRE, Stratus, kagent, K8sGPT and Pi were studied as design references only; no code from them is in the repository ([source comparison](../research/investigation-source-comparison-2026-09-07.md), [secondary comparisons](../research/upstream-secondary-benchmarks-2026-09-06.md), [runtime selection](../research/runtime-selection-2026-09-07.md)).

## Run, not redistributed

The lab and the product use these at pinned versions; the repository references them but does not include their code.

| Component | Pin | Where pinned |
|---|---|---|
| OpenTelemetry Demo Helm chart (appVersion 2.0.2) | `0.37.8` | `scripts/kind_lab.py` |
| kube-state-metrics chart, Alertmanager chart | `8.6.0`, `1.24.0` | `scripts/kind_lab.py` |
| nginx (lab read-only Prometheus proxy) | `1.28.0-alpine` | `scripts/kind_lab/prometheus-readonly-proxy.yaml` |
| Docker Engine, kind, kubectl, helm (Linux lab install) | `29.2.1`, `v0.33.0`, `v1.37.1`, `v4.3.0` | `scripts/kind_lab/install_linux.sh`; [f9-clean-install evidence](../evidence/f9-clean-install-2026-11-09/README.md) |
| PostgreSQL image | `postgres:17.9` by digest | `deploy/compose/compose.yaml` |
| Python base image, uv | `python:3.12.13-slim`, `uv:0.10.8` by digest | `Dockerfile` |
| Python libraries (FastAPI, Jinja2, Uvicorn, Pydantic, psycopg, psycopg-pool, SQLAlchemy, Alembic, OpenAI client, OpenTelemetry SDK, LangSmith, tokenizers, httpx2) | exact versions | `pyproject.toml`, `uv.lock` |

## External services

- **DeepSeek API** (`deepseek-flash`): the investigation model.
- **LangSmith**: trace and experiment storage for lab runs ([ADR-0006](../adr/0006-trace-evidence-and-backlog.md)); off by default in the product.
- **z.ai `glm-5.3-flashx` and OpenAI `gpt-5.6-luna`**: the two uncalibrated binary judges in the F1 exam.
