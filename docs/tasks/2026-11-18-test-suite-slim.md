# 测试套件精简

- 状态：进行中（PR1 #709 待合并；PR2 已在本地完成，等 PR1 合并后开）
- 更新日期：2026-11-18
- 依据：AGENTS.md「验证与汇报」（外部 `IncidentScenario -> IncidentOutcome`）、[PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md)、用户方向 2026-11-17「测试少而有用」与 2026-11-18 裁决（见合同 r1）
- 工作区：`/Users/shenghuikevin/dev/AI/poa-test-slim`，分支 `chore/test-suite-slim`（起点 `origin/main` e4020302）

## 目标与范围

把约 7,400 个测试函数（26 万行，产品代码的 3 倍）收到只剩高维度测试：①场景驱动验收（含 feature_list 每个验收步骤）②工作台 HTTP 驱动加浏览器路径 ③一个集中的表驱动安全边界套件 ④最小 PG 存储与迁移合同 ⑤报告校验合同。其余（`_impl` 镜像、按 issue 编号的回归堆、评测/实验环境/spike 工具测试、内部单测、措辞钉子）默认删除，除非守着别处没守的不变量。目标 ≤600 个测试函数、PR CI 墙钟 ≤6 分钟。不改产品代码（`opspilot/`）与迁移；`scripts/` 不动。

分两个 PR，不叠：
- PR1：CI 的 PG 选择改为 `pg` 标记，补上漏跑的 PG 文件；新建 `tests/safety/`；变异清单与 `make mutation`。
- PR2：大删；删前删后跑同一份变异清单；搬跨文件 helper；保留的慢用例去掉墙钟等待（测试侧回填时间、测试配置缩短超时）。

## 前提与完成条件

- 前提：本机 swap 紧，只跑定向文件，全量交 CI；PG 定向测试与 PG 变异用 scratchpad 里的临时 PG 17（端口 55471，任务结束停止）。
- 完成条件：PR1 与 PR2 的 CI 绿、独立审查完成；变异清单在删前删后全部 `killed`；PR2 正文附删除量、passes 旧→新映射表、CI 前后耗时。

## 合同（r1）

| 编号 | 裁决要点 | 出处 |
|---|---|---|
| R1 | 只删测试，`scripts/` 工具脚本保留（`scripts.m0.postgres_lab` 是测试基础设施） | 用户 2026-11-18 |
| R2 | 功能验收收拢到场景驱动后，feature_list `passes` 的证据可改指新文件；条件是每个验收步骤在新文件有对应用例且过变异检查；PR 正文附旧→新映射表 | 用户 2026-11-18 |
| R3 | 暴露出的失败逐个判断修测试还是删；产品 bug 只记 issue 候选，不修产品代码 | lead 2026-11-18 |
| D1 | `pg` 标记由 `tests/conftest.py` 自动判定（`tests/integration/`、模块或所用 fixture 读 lab PG 门控变量、skipif 理由是 lab PG 门控），不在 160 个文件里逐个手写；理由：PR2 删掉大部分文件，自动判定对新文件同样生效 | Agent 自定，可逆 |
| D2 | `OPSPILOT_PG_SELECTION_GUARD`：checks 中 lab PG 跳过、PG 作业中门控变量缺失导致的跳过都直接失败；理由：防止测试漏选后哪里都不跑 | Agent 自定，可逆 |
| D3 | 变异工具放在 `tests/mutation/`（不改 `scripts/`），Make 目标 `mutation`，不进 CI 必需检查 | lead 2026-11-18 + Agent |

## 必要上下文

- 审计（2026-11-18，scratchpad 报告的结论）：
  - 现状：`_impl` 147 个文件（与独立验收行覆盖几乎重合）；评测、实验环境与 spike 工具测试 142 个文件、6.8 万行；198 个文件跨测试文件 import helper。
  - checks：最慢 50 例里 97% 是评测工具链测试，原因是每个测试都重建 git 工作区。
  - PG：≥1s 的用例约占 PG 测试时间的 68%，主要在等真实 deadline（`wait_past`）。
- CI 选择漏洞（PR1 修复前）：PG acceptance 用 `grep M1_DURABLE_POSTGRES` 选文件，经驱动 fixture 门控的 `tests/acceptance/test_workbench_conversation.py`（24 例）、`test_686_replies_key_wrapper.py`（7 例），以及 8 个顶层或 unit 目录下的 PG 文件和 `tests/test_acceptance_audit.py`（20 例）在任何 CI 作业里都不运行。

## 执行进展与证据

### PR1

- **PG 选择**：
  - `tests/conftest.py` 加 `pg` 标记与选择守卫；`ci.yml` 中 checks 跑 `-m "not pg"`，`pg-integration` 跑 acceptance 与 browser 之外的 `-m pg`，PG acceptance 跑 `-m pg tests/acceptance`，browser 跑 `-m pg tests/browser`。
  - PG 作业设 `OPSPILOT_TEST_DATABASE_URL`。
  - 本机收集对比：旧选择的文件全部在新选择里；新增 11 个文件。
  - 新增文件在本机临时 PG 上的结果：`test_workbench_conversation`、`test_686_replies_key_wrapper`、`test_acceptance_audit` 及 8 个顶层或 unit PG 文件共 57+31 例通过（`test_acceptance_audit` 要求库已迁移，CI 的迁移步骤先于测试）。
- **安全套件 `tests/safety/`**：

  | 文件 | 守什么 | 用例 |
  |---|---|---|
  | `test_safety_tool_gateway.py` | 按 `otel-demo-ext` 注册表枚举全部工具：只读注册、写动词拒绝、未注册目标与越权窗口在发请求前拒绝、K8s Secret 各种拼写拒绝、PromQL `offset`/`@`/嵌套 lookback 越窗拒绝、挂起与在途人工决定（结果只作历史）、源内容与源错误回显里的凭据（userinfo、赋值、带引号、Authorization、Bearer）不进模型视图与证据 | 199 |
  | `test_safety_recovery_authority.py` | 按全部已发布 HealthProfile 的每个必需信号枚举：缺失、各种非 ok 状态、点数不足、过旧、窗口过期、越界都不能确认恢复；未绑定 profile 不确认；带 healthy 字样但缺信号的样本不确认 | 76 |
  | `test_safety_human_control_postgres.py` | 工作台中途 pause/cancel 的决定压过 Run 的迟到报告；暂停的事故不会被 claim | 3 |
  | `test_safety_runtime_roles_postgres.py` | 目录权限等于 `RUNTIME_GRANTS`；独立于声明钉住：worker 不写已审知识，web 不能签发租约，删除只限两条已声明路径，没有 TRUNCATE 与 CREATE，observer 只能改 lifecycle | 10 |

- **独立审查（#709，MERGEABLE）后的三处修复**：
  - `pg-integration` 安装 PG 17 服务端并把 `/usr/lib/postgresql/17/bin` 加进 PATH。这让 #392 roles、#429 downgrade、F11 U2 register 这 7 例自起 initdb 集群的用例在 CI 里真跑。守卫也把它们的「self-owned initdb binaries/user unavailable」跳过算作漏洞。这些文件 PR2 会删，修复照样落在 PR1。
  - gateway 自己配置的 `K8S_TOKEN`/`OTEL_TOKEN` 加进凭据断言，补了一条变异 M19：模拟 transport 把自己的 token 回显进源错误，结果被抓到。源错误回显测试改用 Prometheus 的错误外壳，并断言回显确实发生，免得断言落空。
  - 守卫把「test PG unavailable」也算作 PG 作业里的失败。
- **变异清单 19 条**（`tests/mutation/mutations.py`）：只对 `tests/safety` 跑一遍，结果 19/19 killed（含 PG 3 条，本机临时 PG）。命令是 `make mutation`，带 PG 环境；结果表附在 PR 正文。
  - 第一轮 5 条存活。M06、M10、M13、M15 的原因是用例没打中那条规则，已补用例（嵌套 lookback 用例、无名 Bearer、伪造 healthy 样本、窗口过期）。
  - M16 存活的原因是 publish 的人工控制栅栏有多层，单删一层会被其余层兜住；已把该条改成同时去掉 publish 判定与 `_lease_revoked` 两层。
- **产品问题候选**（不修）：带转义引号的 JSON 文本（日志里再次序列化的 `\"token\": \"v\"`）不被脱敏；`redact_credentials` 只处理键名本身用转义拼写的情况。已开 [#710](https://github.com/kevinWangSheng/production-ops-agent/issues/710)，PR2 之后修。

### PR2

- **删除量**（相对 PR1 的树）：

  | | 删前 | 删后 |
  |---|---|---|
  | `tests/` 下的 .py 文件 | 598 个 | 96 个（其中测试模块 55 个） |
  | 行数 | 250,806 行 | 37,156 行 |
  | 测试函数 | 7,033 个 | 485 个 |
  | 收集用例 | 约 15,700 个 | 1,052 个（其中 PG 363 个） |

  另删 17 个只服务已删测试的 `*_AMBIGUITIES.md`。
- **留下什么**：
  - 安全套件（`tests/safety/`，加上报告校验表与 Observer 独立性）。
  - 工作台：`test_workbench_conversation`、`test_workbench_p3_late_results`、浏览器路径、`test_m1_incident_control_postgres`。
  - 各功能验收步骤文件：F2、F6、F7、F11、F12、F13，F3 的 PG 验收。
  - F8/F9/F14 的确定性合同文件：386 日志、413 健康、428 outbox、429 降级、392/394/395 部署与文档、385 上游映射。
  - 迁移：`test_schema_migrations_postgres` 与几个小的 schema 结构守卫。
  - 报告校验：`test_m1_report_contract_citations`、`test_686_report_json_contract`。
  - 其他结构与凭据守卫：`test_architecture`、`test_knowledge_store_callers`、`test_m1_prometheus_credentials`、`test_sql_column_order_independence`。
- **helper 搬家**：
  - 被保留测试 import 的 18 个测试模块，去掉测试函数后改名为 `*_support.py`（以数字开头的加 `issue` 前缀），所有 import 改到新名。
  - `tests/test_m1_otel_demo_contract.py` 原地去掉测试、保留文件名，因为 `scripts/f7_hostile_live.py` 从这个路径 import（`scripts/` 不动）。
  - 被 ruff 当作未用而删掉的转出口名字，已按原文件的 import 补回。
- **去掉墙钟等待**（只改测试，不改产品）：
  - F11：新增 `Env.expire_deadline()`。在超级用户、`replica` 模式下把发布与其 session 的 `deadline_at` 移到当前时刻之前，`released_at` 同步平移以满足 CHECK；用它替换 5 处 `wait_past`。F11 四个步骤文件本机耗时从约 120 s 降到 25 s。
  - F2 第 3 步：工具超时在测试里改为 2 s（替换 otel-demo 注册项，Run 接受前替换，版本一致），30 s 降到 2.5 s。
- **CI**：PR1 为 7 例自起 initdb 集群的用例加的「安装 PG 17 服务端」步骤，在 PR2 中随这三个文件（#392 roles、#429 downgrade impl、F11 U2 register）一起删掉；保留的测试都不调用 initdb。
- **变异清单 25 条**（PR1 的 19 条，加 M20–M24 报告校验、M25 Observer 独立性）：删后对 `tests/safety` 跑，25/25 killed（含 PG 3 条），见 PR 正文。
  - M20–M24 在删前的两份报告测试文件上只能抓到 M20、M23，所以新加了报告校验表。
  - 删前对旧的全量套件跑变异超出本机内存限制，未执行。
- **passes 证据**：
  - 各功能步骤原本作为证据的确定性文件全部保留、原名不变，旧→新映射表在 PR 正文。
  - 删掉的是子 PR 级的独立验收和实现者测试（例如 F11 PR-2b 至 PR-5b、F2 B1 的 PG 文件、`test_f2_query_deadline_acceptance`）。这些行为由保留的步骤文件和 `tests/safety` 覆盖。
  - 历史任务记录里对已删文件的路径引用保持原样，属于历史记录。`docs/development.md` 里仍描述现行把守的引用已改为新文件。

## 下一步与交接

- PR1：Draft，等 CI 与独立审查。
- PR2：PR1 合并后把 main 合进分支 `chore/test-suite-slim-2`，开 Draft PR，用 CI 测删后耗时。
- 运行中资源：scratchpad 临时 PG 17（端口 55471，数据目录 `scratchpad/pgdata`），PR2 完成后停止。
