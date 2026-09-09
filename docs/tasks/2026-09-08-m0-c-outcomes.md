# M0 C：公开验收合同与开发用例

- 状态：本轮修复与独立复验已完成，公共基线已合入并复测；PR #7 待用户审核。最新提交 CI 以 PR 实时 checks 为准；更新日期：2026-09-09。
- 批次与授权：[索引](2026-09-08-m0-batch.md)，本地可逆 M0 实施测试及 PR；不合并、不联网实验。
- 依据：SPEC、C3 §5/7/11–13、M0 §1–7；F1/F2/F7/F8/F14 相关机制前提，不更改 steps/passes。
- 工作区：协调者从共同基线创建 `production-ops-agent-m0-c` / `chore/m0-c-outcomes`。

## 目标、接口与归属

细化 IncidentScenario/IncidentOutcome 的公开 schema、开发 fixture 与确定性 outcome 断言；正常/失败/缺证据及 Agent/evaluator 输入分离；复用研究并核对实际上游和 OTel 版本/配置源码，标明静态草案与未运行。

专属文件：scripts/m0/outcomes.py、tests/test_m0_outcomes.py、tests/fixtures/m0/outcomes/、docs/testing/m0-public-outcomes.md、docs/testing/m0-upstream-mapping.md；本任务记录及 docs/evidence/m0-c/。共享接口遵守[合同](2026-09-08-m0-shared-contract.md)。依赖锁、CLI/config、ROADMAP、批次状态由协调者修改；需依赖时报告具体版本理由，不自行更新锁。PR base 为 chore/m0-batch-baseline，依赖基线未合并变更，不能算本 PR 独有。

## 实验前提、步骤与完成条件

执行前核查 Git、完整 SPEC 与上述依据；不得读取真实 .env，合成资料不包含业务或保留集。先记录所选版本、实验命令/输入/预期及失败处置，再实施与运行。运行 schema/fixture/非法 outcome/权限/证据来源/执行状态断言；保存版本和来源映射证据。同时保留独立 ReleaseObservation 身份、正常发布无 Incident、调查结论与独立健康观察分开；健康断言必须核对目标/规则版本/时间窗/新鲜度/样本并拒绝缺测认证。禁止制作/读取私有保留集，不冻结最终统计/质量阈值，不扩展平台。所有失败保留、修复后重测；环境不具备单列证据不足。自测后由全新上下文独立审查，处理发现后提交，协调者推送/建 PR 并检查 CI。

## 资源、进展与交接

A/C 仅短时 Python 测试及公开资料查询；B 独占本批唯一重型环境，不干扰其他项目。真实模型和 trace 次数必须为 0，付费总额未授权，不复制凭据。独立数据库不增加真实授权额度。
以下为本轮实际结果；PR/CI、独立审查由协调者接续，合并仍待用户。

## 执行前实验合同（2026-09-08）

工作区 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-c`，分支 chore/m0-c-outcomes，基线 2a1cc49，起始干净。已读完整 SPEC、C3、M0、共享合同及 F1/F3/F6/F7/F11/F14 原步骤；接口独立审查由协调者完成。
问题：公开外部输入/结果能否拒绝缺证据健康、自述成功、错误引用和越权动作，同时保留正常发布独立身份？
方法：Pydantic 严格公开 DTO；外部 evaluator 另持可见性判定、期望状态、已捕获证据与独立观察；合成 JSON 开发 fixtures，通过 pytest 从 JSON 入口重放并变异不合法结果。真实 Agent、模型、目标环境均不运行。
版本：Python 3.12 与基线 uv.lock（安装后记录精确版本）；schema m0-public-v1；evaluator m0-deterministic-v1。现有传递 pydantic 由协调者提升为固定直接依赖，不在 C 改锁。
命令：make setup；定向 pytest；make check；记录原始输出于 docs/evidence/m0-c。通过条件：正常/失败/缺测 fixtures 合同通过，伪造身份/引用/执行状态/健康/权限结果被拒绝，Agent DTO 无 evaluator 字段。失败保留原始日志、修复后重测。源码核查固定 upstream SHA 与 OTel release commit，并保存来源 URL/hash；不执行上游或工作负载，不声称 F14 baseline。

## 本地结果与交接

- `make setup` 按基线锁成功；公共依赖变更由协调者提交0f25fe8并正常合入，C没有改锁。最终同步基线e5a971c。Python 3.12.13、Pydantic 2.13.5、pytest 9.1.1、Ruff 0.16.6；精确清单见 [versions](../evidence/m0-c/versions.json)。
- 6份公开开发JSON、严格schema、37项新增测试；make check最终84 passed，Ruff和锁检查通过：[最终原始输出](../evidence/m0-c/check-final.txt)。
- 首次编辑 lint 报单行复合语句，格式化消除；首次make check因下载的上游.py原文件被全仓Ruff扫描失败，保留 [check-first](../evidence/m0-c/check-first.txt)，将原样来源重命名.py.txt且更新清单路径（不改其内容/hash）；第二次80 passed；增加外部审计遗漏和人工关闭区别测试后84 passed。
- 网页工具对GitHub tag API返回safe-open错误，改用公开urllib只读API成功；OTel 2.0.2对应63649d6d6a59de88fb421b88c3c3a6185b6d21ad，Holmes固定既有SHA。源码/许可证/hash见 [sources](../evidence/m0-c/sources.json)，配置映射 [草案](../testing/m0-upstream-mapping.md)。
- 公开合同见 [说明](../testing/m0-public-outcomes.md)；JSON Schema归档可重生成。Agent字段投影不等于OS隔离；合成signal verdict不是实际遥测计算；check_outcome只检查合同一致，不评价自然语言因果正确。
- 未执行模型/trace/重型环境/权限隔离/真实基线/故障注入/soak，未读取.env或保留集，未冻结最终阈值、未更改passes或SPEC门槛。没有专属后台服务；.venv保留用于复验。
- 下一步：协调者启动全新上下文独立审查，处理发现后推送任务分支/建PR/等待最新CI；用户审核合并。任务本地完成与M0退出、产品验收严格区分。

## 独立审查修复（2026-09-08）

依据 review_c 的 [独立审查](../evidence/m0-c/independent-review.md) 处理 C-R1/C-R2 两项P2；本轮只改对应最小支持结构与独立观察证据完整性，重新复核 SPEC 相关门槛与约束，未扩大权限或实验。
新增反例先运行：[修复前失败原始输出](../evidence/m0-c/review-regression-before.txt)。supported 现要求至少一个带引用的 fact，其引用继续通过既有完整证据校验。独立观察现校验所用捕获证据hash并拒绝重复ID；check_outcome对捕获证据独立记录hash错误，即使报告省略也不可绕过。未要求Observer证据进入AgentInput。
新增合法supported正例、无引用假设/建议/拒绝假设反例、隐藏于报告之外的损坏证据，以及仅Observer拥有完整/缺失/重复证据的边界测试。schema字段未变，序列化JSON Schema对照仍一致，无需更新fixture/hash；源码SHA由Git提交固定。
第一次修复后make check在reviewer新增Markdown代码块的格式检查处失败，见review-fix-check.txt；文件属reviewer，已联系其自行格式化，没有改审查结论。协调者已格式化审查文件，reviewer确认无需额外修改；定向45 passed，最终make check全仓92 passed、Ruff与锁检查通过，完整重测结果见review-fix-final.txt；仍待同一独立审查者复验关闭发现，不自行记独立通过。

## 独立复验

全新上下文review_c两项P2原反例均复验拒绝；合法supported、独立观察和partial/inconclusive正例通过，定向45 tests/完整92 tests。[报告](../evidence/m0-c/independent-review.md)。7份来源hash及OTel tag独立核查一致。本地实现与本批独立审查完成；PR/CI待协调者回读，未合并，不代表M0退出/正式评测或产品验收。


## PR #7 GitHub 发现修复（2026-09-09）

本轮执行前重新检查原 worktree（起始干净）及 SPEC 门槛/排除项/证据/控制/费用/验证要求；依据 C3 §3/10、F1/F7/F11 原验收、M0 §1/3/5、共享合同。范围仅为公开 DTO/合成断言及相关测试文档，不改 steps/passes，不读取真实 .env，不调用模型或 trace。原分支继续使用，公共基线由协调者统一合入。

实验合同：重放审查给出的匹配 audit/outcome 越权授权、延迟评估掩盖提前 healthy、异常无 Incident 反例；修复后要求拒绝，并保留拒绝尝试、正常按期发布及已关联异常正例。记录原始失败与 make check 输出；完成后由未参与修复的新上下文 agent 复验，不以实现者自测替代独立结论。

- [3965230167](https://github.com/kevinWangSheng/production-ops-agent/pull/7#discussion_r3965230167)：把 forbidden capability 授权判断从 executed 条件分离，mutate/release_gate 的 authorized=true 即拒绝；覆盖四种授权/执行组合与 audit-only/outcome-only/both 三种来源。
- [3965230180](https://github.com/kevinWangSheng/production-ops-agent/pull/7#discussion_r3965230180)：公开 outcome 增加 release_completed_at，与独立 evaluator 的 observed_release_completed_at 对照，按真实终结时刻核对最短跟踪/原 deadline/评估时刻及终结前可用的独立观察证据。新增早结束晚评估、缺时间/审计、伪造时间、未来/超 deadline、短跟踪及终结后捕获证据反例和延迟评估正例。
- [3965230184](https://github.com/kevinWangSheng/production-ops-agent/pull/7#discussion_r3965230184)：anomalous 必须有 linked_incident_id，不能因 evaluator 同样漏配而通过；有合法关联的正例保留。
- 新必需字段改变公开输入，schema/evaluator 升为 m0-public-v2 / m0-deterministic-v2；六份公开 fixture 明确更新，当前 Schema 另存 *.v2.schema.json 并测试与 DTO 一致。原 v1 Schema、版本、检查和独立审查历史原样保留；不把旧结果作为 v2 验证。
- 权限/关联反例修复前输出见 [pr-review-regression-before](../evidence/m0-c/pr-review-regression-before.txt)，7 failed / 63 passed。第一次新增完成时间反例与全部定向测试：80 passed。
- `make check` 通过：127 passed（其中 outcome 定向 80），Ruff/format/锁检查通过；原始输出 [pr-review-check](../evidence/m0-c/pr-review-check.txt)。
- 本轮独立复验、基线合入后复测、提交/推送及最新 CI 由协调者接续，当前未宣称完成。实验仍仅公开合成合同，不证明真实网关权限、连续流量、数据库状态转换或产品验收。

- 本轮独立复验已完成：全新上下文 Agent 亲跑 `make check` 127 passed，额外检查独立审计与完成时间边界、权限、正常/异常发布反例及 v2 schema 一致性；无待修复发现。见[报告](../evidence/m0-c/review-2026-09-09.md)。下一步合入通过复验及 CI 的公共基线、复测并推送原 PR #7；不合并 PR 或改产品验收。

## 公共依赖合入与最终交接

#4 公共基线 `92eb7d0` 经独立复验及远程 CI 成功（run 34325057891）后，通过普通 merge 合入本分支 `f5e75ce`，无冲突、未复制共享实现。`make check` 137 passed；见[受检提交与结果](../evidence/m0-c/baseline-merge-check-2026-09-09.json)。本轮未新增数据库实验或真实模型/trace调用。后续交用户审核原 PR，合并须明确授权；最新检查与评论处置以 PR 描述和实时 checks 为准。前文各阶段的待办为当时记录，本节为当前交接。
