# 定向重跑（#772、#775、#776 合入后，2026-11-22 08:44–10:58 UTC）

性质：有界真实运行（opspilot-tool-1），只跑 OpsPilot 侧与 SREGym p13，不跑 Holmes 侧；DeepSeek 真调，judge glm-5.3-flashx，`--trace lab`。不是生产证明。

- 代码：主机本地分支 `rerun/2026-11-22`（不推送），`origin/main` `d3c7558d` 依次 `git merge --no-edit` `origin/fix/runbook-report-naming`（#772，`68899df2`）、`origin/fix/redaction-secretref-names`（#775，`9b9bad37`）、`origin/fix/report-incomplete-boundary`（#776，`e2fd3b6c`），合并提交 `8d805153`。#776 与前两者在三个测试钉值文件冲突（`tests/fixtures/c6b_sql/pg_mysql_pins.json`、`tests/fixtures/c6c_mongo/pins.json`、`tests/test_tool_expansion_c6a_contract.py`），本地取 HEAD 一侧以便提交，钉值测试在该分支上不成立、未跑；产品代码五个文件三个 PR 互不重叠，自动合并无冲突。独立 worktree `~/poa-rerun`，venv 用 `uv sync --locked`；web、worker 均由该 worktree 的 `.venv` 启动，每遍 Run 行 `versions` 与 worker 日志的 `prompt_revision` 一致（worker 在不一致时拒绝领取）。
- 命令：`/home/opspilot/rental-logs/rerun/{o_round,lane,b1x1_split,g_runner,es_up,es2_up}.sh`（主机，不入库）。两线：B（端口 30000+，ES 8.19.25 在 `localhost:30920`）、S（35000+）；S 线跑完 C2/C5/C6/C1 后在第二个 ES（`localhost:30921`）上分担 B1 单遍题的前 9 题（轮 `rerun-o-b1-x1`），B 线跑后 8 题（轮 `rerun-o-b1-x1b`）。C5n（259_loki、257、258）不带 `--k8s-env-file`，其余都带。C5/C6 在 S 线串行。SREGym p13 与两线并行（独立集群 `kind-sregym`、独立端口）。
- 开跑前：lab K8s 只读 token 已于 2026-11-21 16:34 UTC 过期，用 `scripts/kind_lab.py k8s-token` 重签（2026-11-23 08:43 UTC 到期）。
- 题量：3 遍 = B1 12 题（259_wrong、264、266、185、261、235、260、262、263、265、267、269）与 C2 6 题；1 遍 = 其余 B1 17 题（不含 280）、C5k 6、C5n 3、C6 7、C1 283；SREGym p13 3 遍。共 88 个 OpsPilot trial + 3 个 SREGym trial，全部 `status: completed`，无缺遍、无环境失败。
- 冒烟（08:44–08:49 UTC）：266 t1、94 t1 各出报告、summary 与 trace，计入正式轮次。

## 结果（原始 k/n，不补跑摇摆题）

对照表：[`compare.md`](compare.md) / [`compare.json`](compare.json)（全量 vs 重跑，每题 k/n）；各轮 `scores.json` 由驱动 `scores` 生成，分母固定按 3 遍计，1 遍轮的 `main` 总分不可直接读；SREGym `scores.json` 的 `main` 按 17 题计，本次只跑 p13，同样不可直接读。

- 3 遍题：B1 30/36（全量同 12 题 22/36），C2 12/18（全量 10/18）。全量的 no_report 题 259_wrong、264、266 本次 3/3、3/3、3/3，185 0/3（三遍都有报告、judge 判 B），261 3/3，235 1/3（全量 2/3），90 2/3。
- 1 遍题：B1 17/17，C5k 5/6，C5n 3/3，C6 7/7，C1 1/1。
- SREGym p13：1/3（全量 1/3）。
- 全量通过、本次未通过：100a_loki_historical_logs 0/1（全量 3/3），产品转人工 `REPORT_INVALID`（Run 终态 waiting_human，judge 未调用）；267_cluster_name_alias 2/3（全量 3/3），t2 judge 判 B。
- 本次全部 88 遍里 no_report 只有 100a 这 1 遍；全量同一批题 no_report 15 遍。

### 100a 追加 3 遍（10:38–10:58 UTC，同一构建 `8d805153`，轮 `rerun-o-c5k-100a`）

- 结果 3/3（judge 均 A，报告均 `completed/partial`，模型请求 12、24、22）；t2 第 1 次机会因上一遍 web 尚未释放 35080 端口记 infra（`PROCESS_EXITED`），驱动按设计重来一次。合计 100a 3/4。
- 首轮失败原因（取自原始 ledger 的步骤，不入库）：报告 JSON 与 schema 均有效（用 `ReportV2` 重放通过），`assessment_status: completed`、`conclusion: partial`，与全量 3 遍同形（全量也是 `completed/partial`，18–19 次请求），不涉及 #776 新增的 completed/incomplete 措辞。拒收来自引用校验：第 19 轮的报告里第 17 条 `recommendation` 引用了不存在的 evidence_id（SOURCE_UNAVAILABLE 的 `traces_search` 操作号拼上编造的后缀），产品回退要求改写；第 20 轮（本 Run 最后一轮）改写后该条仍引用 `b3c1bf99-…-t0`，即那次不可用查询的操作号而非 evidence_id，仍不通过，Run 以 `REPORT_INVALID` 转人工。属于与全量 235 t2 同类的偶发引用错误。

## 各 PR 的对应 Run

| PR | Run | summary | trace |
|---|---|---|---|
| #772 报告点名所取 runbook | C2 94_skill_transparency t1（报告摘要写明 "runbook fetched and followed: database-troubleshooting"） | `opspilot/holmes-evals-rerun-o-c2/94_skill_transparency/trial-1/summary.json` | [LangSmith](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/5dd61723-33d4-4828-9c7b-d4da2eb3ece6/r/00000000-0000-0000-fa63-0cb82fcdbc84?poll=true) |
| #775 Pod spec 显示 Secret 引用名 | SREGym p13 t1（报告点名 Secret `product-catalog-db-conn`，凭据值仍显示为 `[REDACTED_CREDENTIAL]`，通过） | `sregym/sregym-lite-rerun/p13/trial-1/summary.json` | [LangSmith](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/d2cf3b94-025d-4839-a5ce-495e7c328668/r/00000000-0000-0000-cc9a-13a4950a4af8?poll=true) |
| #776 completed/incomplete 边界 | B1 266_toolset_disabled_vs_no_data t1（报告 `assessment_status: completed`、`conclusion: inconclusive` 被产品接受，judge A；全量 3 遍均被拒） | `opspilot/holmes-evals-rerun-o-b1-x3/266_toolset_disabled_vs_no_data/trial-1/summary.json` | [LangSmith](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/0e2e1cff-8cdb-448b-8ec4-3dffb3826de9/r/00000000-0000-0000-df13-810d091ec059?poll=true) |
| 未通过：100a no_report | C5k 100a t1（`REPORT_INVALID`：recommendation 引用了不可用查询的操作号，见上节；追加 3 遍 3/3） | `opspilot/holmes-evals-rerun-o-c5k/100a_loki_historical_logs/trial-1/summary.json` | [LangSmith](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/d496eb34-efe2-4a66-b28d-cb9b0dade52e/r/00000000-0000-0000-a0b0-3b9d72303a25?poll=true) |

## 费用

- DeepSeek（token × 单价，94 个 Run 的 summary `run.tokens`，含 100a 追加 3 遍）：缓存命中 48 055 552、未命中 4 017 467、输出 1 407 677 token；Run 开始时刻 2026-11-22 08:45–10:58 UTC 为周日，全部闲时；单价取自 https://api-docs.deepseek.com/quick_start/pricing（2026-11-22 读取，deepseek-flash 每百万 token 命中 $0.003、未命中 $0.15、输出 $0.60，高峰为周一至周五 01–04、06–10 UTC，价格翻倍）：约 **$1.59**（若全按高峰价 $3.18）。不含 Z.ai judge。

## 披露

- 凭据扫描：104 个文件、15 个凭据值（DeepSeek、LangSmith、ZAI、两组 ES 工程师 key 与 elastic 口令、K8s token、Prometheus 口令与账号名、两个 lab token），0 命中；正对照命中。
- p13 报告引用了题目 ConfigMap `init.sql` 中的 lab 数据库口令原文（SREGym 注入的故障数据，非本项目凭据）；全量已入库的 p13 summary 同样含该值，非本次新增。
- 未跑：Holmes 侧、280（已归类为时间盒受限）、C1-U2 todo-hard 对照、摇摆题补跑。
