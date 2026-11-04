# S7b：HolmesGPT 数据集 runner

- 状态：进行中
- 分支 / worktree：`feature/F14-s7-holmes-dataset-runner` / `~/dev/AI/poa-s7-holmes`
- 依据：F1 收尾裁决（仓库外 `~/.agent/handoffs/opspilot-rulings-2026-11-04-f1-closure.md`，含 07:40 PT 修订）；预审 `opspilot-prereview-f1-closure-plan-2026-11-04.md` §1 C、§2.2 S7、R7、R15、§3.3；[收口合同](2026-10-27-m3-closure-contract.md) V2-R7、V2-R12。

## 目标

让 HolmesGPT（`a0009195`，镜像 `opspilot-f14/holmesgpt:a0009195`）回答 v2 数据集的问题入口题，条件与 OpsPilot 相同：同一问句与时间窗、同一只读代理账号 `holmes` 与工具（8 个 Prometheus 工具 + GET-only `jaeger_request`）、OpsPilot 冻结预算；结果按开发轮 summary 的槽结构（`upstream` 行）输出，告警入口题标「不适用」不进分母。

## 系统在做什么

- 入口：验收约定的 `scripts.eval_holmes_dataset`（`select_scenarios`、`holmes_plan`、`result_row`，独立验收测试 `tests/acceptance/test_s7b_holmes_dataset.py`）；配对排程（S7c）进程内调 `run_attempt(spec, answer, attempt_id=, slot=, raw_root=, deadline=, kind_node=, proxy_log=)`，返回 `status` ran/setup_failed/aborted、原因码、起止时刻、`holmes_json` 路径与 sha256；CLI 是同一函数的薄封装。
- `python -m scripts.holmes_baseline.dataset run`：在调用方已持有窗口、已注入并等待 lab 侧注入器（flagd、OTLP span）生效之后跑一次 Holmes。它只做 Holmes 侧的两种注入：
  - 缺测题：配置里把对应数据源改成 OpsPilot 用的同一个回环地址 `127.0.0.1:9`，并把 `127.0.0.1` 加进 `NO_PROXY`，让连接在容器内被拒（同 F14 第 3 步复现）。
  - 模型故障题：Holmes 的出口代理对模型 API 的 CONNECT 回 `503`：`always_503` 每次都回，`first_503` 只回第一次，之后正常建隧道。
- `python -m scripts.holmes_baseline.dataset summarize`：读各 attempt 的 `summary.json`，每题 `--n` 个槽（默认 3），按 regression/capability 分组计数。

## 可逆默认（Agent 自定，一行理由）

| 编号 | 默认 | 理由 |
|---|---|---|
| H1 | runner 不碰 lab：flagd 与 OTLP span 由配对排程（R7）注入、恢复与洗净 | R7 要求同一次注入两系统先后跑，注入只能有一个主人 |
| H2 | 503 落在 CONNECT 层，不是 API 的 HTTP 503 | 模型 API 走 TLS 隧道，不做中间人就只能在 CONNECT 回应；summary 的 `disclosure` 写明 |
| H3 | 缺测 URL 复用 `eval_live_lab.UNREACHABLE_URLS` 原文 | 两边同一地址；Holmes 若因前置检查停用该工具集，这是上游行为，照实记录 |
| H4 | 注入器白名单复用 OpsPilot 的 `validate_injectors` | OpsPilot 驱动拒收的题 Holmes 也拒收 |
| H5 | 提示词沿用 F14 run1 形状：`stimulus.question` 加一行 `Investigation window (UTC): <提交前 300 s> to <提交时刻>.` | F14 r1 已用过；问句与时间窗与 OpsPilot 相同（V2-R15） |
| H6 | 槽的 `final_state_expected` 只作信息列，主判仍是两家 judge（U3） | Holmes 终态只有 completed/failed/budget_exhausted |
| H7 | judge 读原始 `out/holmes.json`：`summarize --judge-inputs` 写 S7a 的 `opspilot-holmes-judge-inputs-1`（每个已开始 attempt 一条，路径相对该文件，无输出为 null，`read_only` 为 runner 判定）；summary 只留 sha256，不含报告正文 | 原始输出不进仓库（F14 r1）；对齐 #469 |
| H8 | `upstream` 维 = `holmesgpt@<commit> config=<基础配置 sha>`（离线可算、缺测 URL 不改它）；镜像 ID 每个 attempt 单独记，一轮内不一致报 `MANIFEST_DRIFT:image` | V2-R7 与验收测试；amd64 与 arm64 不能混在一轮 |
| H9 | `dataset` 维 = `<数据集目录名>:<dataset_tree_hash>` | R8 前缀 |
| H11 | `read_only` = 代理日志里 `holmes` 账号全是 GET，且 Holmes 记录的 HTTP 工具调用全是 `HTTP GET`；`completed` = 终态 completed（Q11） | Jaeger 走 relay 不进代理日志，所以另看工具调用描述 |
| H12 | `deadline` 早于冻结墙钟时把墙钟截到 deadline，被它杀掉记 `aborted`/`DEADLINE`；墙钟预算内超时仍是 `ran` + `budget_exhausted` | 区分排程截止与预算耗尽 |
| H13 | 配对接口 `scripts.holmes_baseline.dataset_runner.paired_runner(deps).run(BaselineRequest)`：Holmes attempt ID = `holmes-` + sha256(round, OpsPilot attempt ID) 前 24 位（确定性、不等于 OpsPilot ID）；原始目录 = `<output_dir>/<该 ID>`，结果的 `output_path` 给出；`submit_not_after` 只在 setup 完、启动 Holmes 前检查（晚了 `setup_failed`/`SUBMIT_LATE`），墙钟仍 7200 s；不抛异常 | 第 1 轮审查 X1（lead 定） |
| H14 | `provider_error`：Holmes 失败、错误日志末 3 行含供应商 5xx/传输失败标记，且经出口代理的最后一次模型 CONNECT 不是注入的 503 | 供 PROVIDER_5XX_STORM 作废规则；只看末尾，避免 Jaeger 不可达的 `Connection error` 误判 |
| H15 | 并行 lab：容器、网络名与 relay URL 加 `kind_lab.SUFFIX`（实例 0 不变）；`upstream` 维按实例 0 的 URL 算 | 同主机多 lab 不冲突，同一轮不因实例不同而漂移；`kind_lab.SUFFIX` 未合入前按实例 0 |
| H10 | `--capture-model` 冒烟模式：假模型端点、假 key、不做 lab 检查；`summarize` 拒收（`ATTEMPT_NOT_REAL`） | 冒烟结果不能混进分母 |

## 证据

- 离线：`tests/test_s7_holmes_dataset_impl.py`（计划、URL 与 `NO_PROXY`、预算等于 `opspilot.investigation.limits`、出口 503 真实 socket 往返、summary 分母与拒收；本机另用 F14 原始输出 run1、jaeger/prometheus 不可达的 real-1 验证解析）。
- 主机冒烟（2026-11-04 08:16 PT，opspilot-f9-1，x86 镜像 `48b16d5532c3`，不调模型、假 key、不开故障开关）：配置与 `docker run` 参数由本分支代码生成。
  - Jaeger 不可达题（`dv2-fcd9eb`）+ 假模型端点：Holmes 正常结束（exit 0），请求核对 `ok`（deepseek-flash、thinking、effort high、65536）；`jaeger_request` 仍在工具列表里。
  - 持续 503 题（`dv2-c39f60`）：出口代理记 1 次 `injected 503`，Holmes 报 `DeepseekException - 503 Service Unavailable` 退出（exit 1），无报告，投影为 `failed`；没有重试，没有连接到达供应商。
  - 由此推断 `first_503` 题 Holmes 也会直接失败（它不重试），未实测，因为第二次 CONNECT 会把请求送到真实 API。
- 已知上游行为（写进 summary 的 `disclosure`）：Prometheus 不可达时 Holmes 启动检查失败、8 个 Prometheus 工具整体不提供（F14 `f14s3-2356-prometheus-real-1` 只有 79 次 `jaeger_request`）；Jaeger 不可达时工具仍在，调用失败。

## 依赖与待决

- #462 已合并并 merge 进本分支：Linux 上 `clock_skew` 读主机时钟、docker 用默认 socket。
- 并行 lab 后缀依赖 `kind_lab.SUFFIX`（S7c 的 `chore/kind-lab-instances`，未合并）；未合并时按实例 0。
- 未确认：Holmes/LiteLLM 对 API 真实 HTTP 503 是否重试（CONNECT 503 不重试；`first_503` 未实测），已写进 summary `disclosure`。
