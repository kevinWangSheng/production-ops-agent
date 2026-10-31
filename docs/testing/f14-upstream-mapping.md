# F14 逐功能上游映射（HolmesGPT `a0009195`）

2026-10-31；F14 第 2 步，合同 [r2](../tasks/2026-10-27-m3-f14-upstream-contract.md#第-2-步合同r2逐功能上游映射表385)。权威表是 [mapping.json](../evidence/f14-upstream-mapping/mapping.json)，本页只做说明；两者不一致时以表为准。上游固定为提交 `a0009195`（tree `e94a57b3`），OpsPilot 一侧对照 main `659bcd9c`。

## 表里有什么

`feature_list.json` 中除 F14 外的 10 个功能、47 个验收步骤，每步至少一行；一步牵涉几个机制就拆几行，每行只归一类。共 77 行：

| 类别 | 行数 | 含义 |
|---|---|---|
| 复用 `reuse` | 13 | 上游机制够用：已移植 8 行（其中归属说明 1 行部分完成），计划移植 4 行，另有 1 行（告警去重）OpsPilot 自写但与上游同一身份口径 |
| 配置 `configuration` | 10 | 上游靠配置就能满足；其中 4 行是同条件基线自己的配置（`scripts/holmes_baseline/`），其余 6 行 OpsPilot 自写同等机制 |
| 已确认缺陷 `confirmed_defect` | 1 | 有 `a0009195` 运行证据 |
| 必要扩展 `required_extension` | 53 | 上游没有，或有但不够；每行写明差在哪 |

OpsPilot 现状：已实现 53 行，部分实现 13 行，计划中 11 行（F8、F9、F1 未完成步骤，只作建议，不改已裁决合同）。

## 按功能

- **F1 评测**（14 行）：结果合同、确定性门、事前校准、保留集是扩展；上游的防作弊出题、10 次重复、事后三类分诊建议在收口线照搬；同条件基线三行是配置。
- **F2 持久事故**（8 行）：告警去重与上游同口径；超时是配置；恢复、关联、背压、人工代际栅栏是扩展（上游会话 worker 退出时把在途会话标为 timeout，依赖 Robusta SaaS）。
- **F3 调查**（16 行）：指标发现、超限回读与基数摘要、有限并行、缺失数据源可见、错误回传、调查方法这 7 行已从上游移植（#307–#311）；ReportV2、证据身份、错误分类是扩展；已确认缺陷在这里。
- **F6 恢复观察**、**F11 发布观察**、**F12 工作台**、**F13 复盘知识**：全部是扩展。上游最接近的是 LLM 判定的 pass/fail 健康检查、按 Deployment 触发的 operator、无状态 `/api/chat` 和从 git 拉取、推送即生效的 skills。
- **F7 安全**（8 行）：外部授权、注入边界、脱敏、暂停、审计是扩展；凭据占位、查询限额和基线的代理对齐是配置。
- **F8 运行可观测**、**F9 交付**（13 行）：健康端点建议照搬上游，许可证与归属按 Apache-2.0 沿用；成本计量是配置（run1 的 `total_cost` 为 0，因 `deepseek-flash` 不在 LiteLLM 成本表）；其余 10 行是扩展，其中上游镜像不可复现（`pin.json`）、Compose 挂载宿主 kubeconfig，都与本项目约束相反。

## 已确认缺陷与候选

- **已确认**（F3 第 4 步）：即时与区间查询成功但无数据时，上游把状态改成 `Failed`，`NO_DATA` 分支走不到（`prometheus.py:L1887-L1896`、`L867-L887`）。run1 第 36 次工具调用就是这样返回 error 的，原文冻结在 [run1-tool-errors.json](../evidence/f14-holmes-baseline/run1-tool-errors.json)（由原始输出生成，sha256 与 `summary.json` 一致，不是新 Run）。OpsPilot 自己的工具单列 `no_data`，不受影响；它影响的是第 4 步基线的可比性。
- **候选**（只有源码观察，第 3 步复现后改类或去标记）：`MAX_OUTPUT_TOKEN_RESERVATION` 只定义不使用；`investigate` 把告警原文直接拼进提示；operator 的发布基线只存在进程内存。

## 怎么核对

- 每条上游引用写成 `路径:Lx-Ly`，引用到的 48 个文件连同 blob SHA 与行数列在 [upstream-files.json](../evidence/f14-upstream-mapping/upstream-files.json)。CI 不拉上游，只校验引用在清单内、行号不超行数。
- 表里 `absence_checks` 命令中的 `<checkout>` 指本机主仓库 git 忽略的 `tmp/upstream/holmesgpt-a0009195`（HEAD `a0009195468ebc55a7dff7d602b61feabe3cda1b`，tree `e94a57b361a00cb2e793cf30d60fe2f4588e26e9`，`git ls-files` 1885 个文件，工作区干净）；子命令默认读这个位置，可用 `F14_HOLMES_UPSTREAM` 改。
- 本机有完整 checkout 时，`.venv/bin/python -m scripts.holmes_baseline.run upstream-files --check` 会重新生成清单并比对，重跑每行记录的「上游没有」搜索（命令与命中数在 `absence_checks`），并检查 OpsPilot 一侧的路径与符号名。
- OpsPilot 一侧写「路径::符号」，不写行号；代码改动后表只对所记 SHA 成立，第 4 步前由那一步的预审决定是否刷新。

## 和既有审计的关系

[2026-10-22 设计审计](../research/upstream-design-audit-2026-10-22.md)与本表同一上游 SHA，上游引用逐条在 checkout 上复核后搬入；四类口径按合同 R8 换算（「该照搬」且已移植记复用，「理由仍成立」「无差距（OpsPilot 更强）」记必要扩展，原文写在 `deviation_basis`）。审计对照的 OpsPilot `a4eea42` 已过时，OpsPilot 一侧按当前 main 重新核对。[M0 映射草案](m0-upstream-mapping.md)对应旧 SHA，正文保留为历史。

上游有而 OpsPilot 没有的能力（K8s 与 bash 工具、MCP、skills 拉取、修复组件）只写在相关行的 `note`，不另列。
