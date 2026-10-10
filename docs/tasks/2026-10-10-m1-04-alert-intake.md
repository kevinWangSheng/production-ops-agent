# M1-04 Alertmanager 告警接入

- 状态：待开始（授权与 r1 已定；合同预审未开始）
- 更新日期：2026-10-10
- 依据：[#167](https://github.com/kevinWangSheng/production-ops-agent/issues/167)；C3 §6「事件接收、去重与持久调度」、§9「页面、认证与人工操作」；PRODUCT-CONSTRAINTS「Evidence and context requirements」；feature_list F1 第 2 步（duplicate-event、wrong-target）、F7 第 5 步（审计重建）；SPEC 2026-10-10 段
- 工作区：授权 PR `chore/m1-04-alert-intake-gate`，`../production-ops-agent-alert-intake`；实施每步一个 worktree，写在执行节

## 目标与范围

事故入口目前只收 `target_id` + 自由文本问题，告警的结构化信息（服务、命名空间、严重级别、开始时间、PromQL）全部丢失，时间框锚定提交时刻。本切片新增 Alertmanager webhook 入口，保留结构化告警，按标签确定性解析目标，时间框锚定 `startsAt`。范围与不在范围内的项见 #167 正文；C3 §14 把「两个入口」排在 M2，本切片是用户提前开放的一部分。不翻任何 `passes`。

## 合同（r1）

用户 2026-10-10 按推荐裁决 #167 的待决 A–D：

- **A 目标解析不到**：接收（2xx），建事故但不绑目标，直接交接给人，不建 Run。不回 422：Alertmanager 一次投递一组告警，422 会连带拒掉同组可解析的告警。
- **B 重复通知、annotation 变化**：只按身份 `fingerprint:startsAt` 判重放，记录新值；不报 `INTAKE_KEY_CONFLICT`，不建新事故。理由：annotation 模板常含 `$value` 等易变值。
- **C resolved 告警**：只记录，并附到已有事故上供人查看；不建事故、不建 Run、不开恢复观察。与 #184 解耦，#184 若改由 resolved 触发观察，另行变更本条。
- **D 服务层**：受影响服务（`namespace` + `workload`）从标签确定性解析，只作焦点信息（Run 输入、工作台显示、登记处置预填观察对象）；不收窄工具授权，模型仍可查同一集成内其他服务。理由：尚无依赖图。

r1 之外的语义点由合同预审列出，裁决后追加为 r2。

## 计划

每步一个 PR，开工前做该步合同预审。

1. 实验环境：kind lab 加 Alertmanager、一条 checkout 错误率规则、带 bearer token 指向工作台的 webhook（chore）。
2. `POST /intake/alertmanager`：webhook v4 解析（告警模型参照上游 HolmesGPT `PrometheusAlert`，`46e3a72`）、逐条处理、去重（B）、目标与服务解析（A、D）、确定性问题模板、resolved 记录（C）、原始 payload 限长入 ledger。功能 PR，用户门。
3. 时间框锚定 `startsAt`：授权框覆盖 `[startsAt - 回看, now]`；输入快照与投影 revision 变化按既有规则处理。
4. 原始告警作为不可信上下文交给模型：`redact_credentials` + 长度上限；同步核查首轮 `question` 未脱敏（`opspilot/investigation/context.py`）。

验收与合同测试由未参与实现的全新上下文 Agent 按 #167 与 C3 §6 编写，覆盖重复通知、错误目标、组内多告警、resolved、超大或恶意 annotation。触碰调查 loop 的步骤（3、4）附有界真实 Run 的 trace 与 `summary.json`。

## 执行进展与证据

- 2026-10-10：授权 PR（SPEC 段、ROADMAP 行、本记录）。

## 下一步与交接

- 授权 PR 合并（用户门）。
- 合同预审：未参与实现的 Agent 只读核对真实代码，按九类列决定表，写入 `2026-10-10-m1-04-alert-intake-contract.md`，用户裁决为 r2。
