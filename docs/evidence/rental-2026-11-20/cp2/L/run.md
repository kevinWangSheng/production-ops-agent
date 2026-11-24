# 检查点 2 线 L 驱动冒烟（2026-11-21 03:33–03:45 UTC）

性质：有界真实运行（主机 opspilot-tool-1，main `8b912caa`，主 checkout 无代码改动；#666 驱动 `round --trials 1 --trace lab`，DeepSeek 真调，judge glm-5.3-flashx；线 L 端口 30000–34999）。每 Run 只有产品默认防循环上限 100 次模型请求。不是生产证明。不含凭据：本目录 9 个文件扫描 6 个凭据值（DeepSeek、LangSmith、ZAI、ES 工程师 key、ES elastic 口令、K8s 只读 token）0 命中；正对照（凭据目录）命中。

- ES 后端：线 L 自起真实 ES 8.19.25（CI 同 digest，`l2-es`，127.0.0.1:30920，开安全认证，口令每次随机）；`--es external` 不带 `--es-readonly-env`，只读 key 由驱动用 elastic 基本认证建（#763 在 main 上）。Holmes 侧同一 ES，用 `http://localhost:30920`（runner 拒绝 127.0.0.1：ES_URL_USE_LOCALHOST，属正确的 fail-closed）。
- C2/C1 带 `--k8s-env-file tmp/m1-kind-lab/k8s-investigator.env`（产品只读 SA）。C6 不带（DB 题）。

| 题 | 组 | Run | 结果 / 判分 | 模型请求 | 墙钟 | summary |
|---|---|---|---|---|---|---|
| 275_es_field_name_correction | B1 | aa72f636 | completed / A | 6 | 41 s | b1-es/holmes-evals-l2-b1-275b/… |
| 254_elasticsearch_dr_test_log_check | B1 | 98fdc7e7 | completed / A | 10 | 71 s | b1-es/holmes-evals-l2-b1-254/… |
| 96_no_matching_skill | C2 | 66b6ed13 | completed / A | 15 | 188 s | c2-runbook/holmes-evals-l2-c2/… |
| 162_get_skills | C2 | 3557f823 | completed / B | 20 | 233 s | c2-runbook/holmes-evals-l2-c2/… |
| 283_todowrite_multistep_audit | C1 | db8e2ad0 | completed / A | 4 | 47 s | c1-todowrite/holmes-evals-l2-c1-283/… |
| 218_mysql_query_performance | C6 | 11651039 | completed / A | 9 | 93 s | c6-db/holmes-evals-l2-c6/… |
| 223_sqlserver_query_performance | C6 | 7e36d2e1 | completed / A | 10 | 125 s | c6-db/holmes-evals-l2-c6/… |
| 254（Holmes 侧，#668） | Holmes 对照 | — | completed / A（opspilot judge 路径） | 9 次 LLM | 59 s | holmes-compare/holmes-compare-l2-holmes-254/…；scores.json 同目录 |

trace 链接见各 summary.json 的 `trace.roots`。

核对清单：
- #666 PR-B 254、275 真实跑：通过（main 上两题都 A）。
- C2（#745）：96 夹具 CR 首次 apply 失败照原样（before_test 报 WhaleWatcher no matches，裁决③，披露）；after_test 删 CRD → 通过（whalewatchers.ocean.io deleted，Run 后集群无该 CRD）；162 `{1..40}` 展开为 40 次（"Attempt n/40"）、`create namespace` 成功 → 通过；产品只读身份读到 Pod（96 kubernetes_jq_query ok 16、162 ok 16 + fetch_pod_logs ok 3）→ 通过；导入审核披露 RUNBOOKS_IMPORTED_REVIEWED → 有；命令带 `--k8s-env-file` → 是。
- C1（#747）：283 工具面有 TodoWrite（offered=true，run_tool_names 读取成功），本次调用 2 次 ok，清单 3/3 completed；before_test 建 4 个 app-283-*、after_test 删 4 个，release check 前后 namespaces_left 0 → 通过；Terminating 命名空间列法 → 未直接观察到 Terminating 状态下的计数（after_test 21 s 内已删完）。
- C6（#756）：MySQL 8 `TLS=require` 连通（database_sql_query ok 12）→ 通过；SQL Server 账号开通与查询成功（sqlcmd 路径可用，ok 11 / error 2 为模型 SQL）、MSSQL_NO_TLS 披露 → 通过；port-forward 10218/10223 → 通过；`kubectl exec` 是否 root、ClickHouse/MariaDB 项 → 本次未跑这些方言。
- #668：上游 classifier 预检（conftest `max_tokens=16`）打 DeepSeek 未失败（setup passed，setup_failed=false）→ 通过；scores 落在 `--evidence-root` 下（#764）→ 通过。R8-13 外网 URL、184/185 判分文本大小 → 未执行。

环境问题（已处理，非代码缺陷）：`docs/evidence/rental-2026-11-20/` 归 root（检查点 1 遗留），第一次 275（round `l2-b1-275`，Run 已 published）冻结时 PermissionError 未冻结；建 opspilot 所有的 `cp2/` 后重跑。
