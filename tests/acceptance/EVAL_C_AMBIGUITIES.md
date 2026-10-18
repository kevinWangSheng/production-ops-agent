# PR-C 独立验收作者记录（r5）

依据：PR-C 合同 r1、公开接口最新 r3（r2 Q1–Q9、§9 勘误、§14
实现对账与测试作者澄清）。本轮已阅读接口 diff 及 §2/6/7/8/13/14。
只改独立验收测试；没有读取另一个 worktree 的实现，没有执行真实适配器。

## 原歧义 1–7：均已裁决

1. **已解决：journal unknown**。setup 前 journal 为 started/unknown，
   product_run_id=null；最终 summary、lifecycle 与 CLI slots 均不得 unknown。
   测试在 journal 现场检查，并对全部已生成的最终状态作断言。
2. **已解决：两种 stale 均为 setup_failed**。InvestigationRequest 的
   submit_not_after 是明确边界。假 Investigator 支持建 Run 前返回
   PRE_OBSERVATION_STALE（没有产品状态），以及返回后由驱动发现 submitted_at
   超时（保留真实 product_run_id、原 Outcome，并在私有 ledger 留 Run ID）。
   两条路径各覆盖 fault/normal；另保留恰好 300s 的有效边界对照。
3. **已解决：已领取 Run 的默认 None 回归归既有 durable 测试**。
   §8 指定的 loop-resume/OTel 等 PG opt-in 测试承担完整回归；当前离线测试
   保留字段默认值、终态 resume 等价与注册 DTO 无凭据检查。本任务没有执行
   这些 PG 测试，接口指定其责任不等于本轮重新验证了其结果。
4. **已解决：只依赖公开入口**。驱动测试只调用 main(argv, deps=...)、
   InvestigationRequest/InvestigationResult 与 attempt_resolver；不构造内部
   RoundConfig、RoundResult、SlotResult，不调用内部轮执行/规划函数。
5. **已解决：dry-run 精确 schema**。断言唯一 JSON 对象的全部键和值：round、
   status=dry_run、按场景主序的 slots（scenario_id/repetition/kind/flag）、
   evidence、ledger；同时严格检查零接缝、零文件写入。
6. **已解决：EnvChecker**。LiveDeps.env.check 是预检第一步；返回
   ENV_UNCONFIRMED 时其他所有接缝均禁止调用，退出 3，零 attempt。
   完整注入 LiveDeps 后设置带 canary 的 M0_ENV_FILE，并守卫文件打开入口，
   不允许驱动读取该环境文件。所有离线测试都有网络/子进程/real_deps 守卫。
7. **已解决：异主恢复意图竞争**。setup 中出现异主 fault-active，精确检查
   LAB_FAULT_FOREIGN、零 ConfigMap 写入、文件原样保留、零 Investigator、
   本槽 setup_failed、余槽 not_started、整轮退出 1；预检发现仍为退出 3
   FAULT_RECOVERY_REQUIRED，不再接受退出 4。

## §9 勘误与 §14 收紧的测试落点

- 两个同字节工具结果均先生成，之后依次生成 delivery；只有首条消息送模。
  引用两个 ID 拒绝，仅首个 ID 接受；缓存歧义拒绝，唯一身份及自带 ID 对照接受。
  refusal 自身零交付；原工具消息独立送模的合法对照仍交付原证据。
- ran 必须重新读取完整、synthetic=false、绑定正确且 Outcome 一致的包。
  新增 synthetic、缺包、内容 hash 损坏、绑定错误、Outcome 不一致的反例。
  正向离线 fixture 用 B2 writer 生成完整包，再按公开 JSON schema 设置私有
  fixture 的 synthetic=false 并重算 ref，交给真实 loader 核验；它有原始输入、
  HTTP 子事件和 finish，全部根目录在 tmp_path。这是确定性测试数据，
  **不代表真实模型调用或 live 证据**，也不绕过或 monkeypatch 录制校验器。
- delivered/authorized/delivery_refs 必须从已核验包重算；测试放入真实离线工具
  证据，并故意伪造 Investigator 的同名字段，检查公开结果与包派生值一致。
- reason 保留首个失败，closure_reason 单独记录恢复失败并成为整轮停止原因；
  用独立 flock 竞争者核验调查、恢复、洗净期间 fault guard 均未释放。
- 恢复写入 FLAG_WRITE_UNKNOWN 时，以 uid、原文 SHA、版本前进三项判定；
  覆盖成功、版本未前进、uid 改变、内容不符和直接 CAS 冲突。
- recover 不改 summary/lifecycle 的任何字节，独占生成 recovery-<UTC>.json；
  通过 attempt_resolver 核验录制、blobs、ledger 的内容与引用 hash。
- 预检拒绝禁止启动 PG、费用查询、Investigator 或 LangSmith 客户端/项目外调。
  身份/flag/端口的预检本身经离线注入探针执行；真实网络始终被守卫禁止。

尚未执行：真实适配器、真实 Run、三类 flag 传播、真实 PG/权限与 trace/read-back，
以及 §8 指定的 PG opt-in 回归。离线验收不能替代这些 live 验收。

## r4/r5 追加合同：已明确，无新增歧义

- 按最新工作副本 diff 及 §3，LiveDeps.provenance 使用固定 commit/clean 的
  离线 ProvenanceReader 假对象，记录调用次数，可注入 LabError；不读真实 HEAD，
  不启动 git，原有网络与子进程守卫保持不变。
- 新测试检查预检全部通过 → provenance.read 一次 → 可选 begin_round 一次 →
  run-starts/journal → 多个槽位调查。两个接缝执行现场都核验尚无开始记录。
- 未实现 begin_round 的假 Investigator 仍可完成；begin_round 抛 LabError 则
  退出 3 并保留固定原因，零 journal/开始记录/PG/余额/调查。
- provenance 抛 LabError 则退出 3，begin_round 不得执行；六类预检拒绝均不得
  读取 provenance 或调用 begin_round。dry-run 对两个新增接缝均零调用。
- 干净且 commit 已知时，公开 summary 和调查请求的 code 维度冻结该版本；
  commit 缺失、dirty 或两者同时出现时仍执行本轮，code 为有原因的 unknown，
  aggregate 保持 MANIFEST_UNKNOWN，不冒充完整基线。

## lead 的 T1–T6 / I3 分诊修正

- **T1**：HTTP fixture 改为完整 DeepSeek chat completion：id/model、单 choice
  的 index/assistant message/finish_reason，以及 usage。reasoning/tool_calls
  同样保留，返回 ModelReply.raw 与产品 parser 的 id/usage 投影一致；未绕过
  B2 的 HTTP → ModelReply 相等校验。
- **T2**：with_evidence 的 binding.default_window 取实际
  scope.default_query_window（为 None 时保持 None），不再冒用授权框。
- **T3**：只对写回 intent.original 的 restore 注入未知结果；下一槽的故障注入
  不再受到上一槽故障脚本的污染。两槽均 ran 的成功断言保留。
- **T4**：缓存通过新的模型调用与额外 delivery 产生；每个物理工具自己的
  parented delivery 原样保留。合法对照明确检查工具数、交付数及最后的空 parent，
  歧义缓存仍要求 IDENTITY_MISMATCH，不再通过删 parent 制造 unpaired tool。
- **T5**：默认 None 的终态回归使用既有 MemoryIncidentStore 离线 fixture，
  真实执行空集合的过期扫描；不再把 Mock 当作可查询 store。仍要求 omitted 与
  explicit None 两次 resume 结果相等且 already_completed，模型/工具零调用。
- **T6**：注册 DTO 用 eval codec.encode 与 repr 检查凭据泄漏，不对
  MappingProxyType 调用 dataclasses.asdict；frozen 断言保留。
- **I3（本轮用户转达的最新明确裁决）**：Q1 轮 manifest 的 registry/permissions
  从同一份快照的静态注册和 scope policy 求 hash，再由 B2 helper 生成独立的
  per-attempt source_manifest。正向例明确要求两层摘要不同仍被接受；另加
  static tools 不同、scope policy 不同的 IDENTITY_MISMATCH 反例。使用 §14
  已命名的公开 opspilot.evaluation.live.registry_hash / scope_policy_hash。
  **T 轮历史冲突**：当时 §14 的 r5 段落写「不再要求」比较这两维，
  测试按用户最新 I3 的「MUST compare」执行；本轮新增 §13 最后一条已明确
  必须比较，并公开了 template_scope。没有修改接口文档。
- r5 预算边界与原因保持严格：600s 端点窗口仍须通过；洗净失败仍断言
  NORMAL_WINDOW_UNCONFIRMED。新增 provenance 默认 None 的离线用例：继续
  执行本轮，code 为带原因的 unknown，网络和子进程守卫保持启用。

## lead 的 G1–G4：fixture 与真实轮策略一致

已全文阅读本轮 §1、§3（新增「轮策略模板」）、§13 最后条件。

- **G1**：recording_context 不再使用通用 m1 的 registration/target/scope。
  快照取 otel_demo_registrations() 的完整真实 DTO；scope 从公开纯函数
  template_scope() 用 replace 只替换 Run 身份字段，授权框保留相同 24 h 长度、
  default_query_window=None，运行 ID 与 scope_id 带实际 Run/epoch。
  真实轮 manifest 与假 Investigator 的静态 registry/policy 因而同源。
- **G2**：with_evidence 以相同 OTel DTO 和同一 scope 构建真实只读 executor，
  control generations 与 scope 一致；真实 OtelDemoTransport 只接既有
  FakeOpener，提供授权窗内的确定性 Prometheus matrix 样本。模型请求、
  ToolRequest 与 binding 均使用真实 metrics_range_query/TARGET_ID。
  fixture 先明确断言 status=ok 与 evidence 非空，再生成交付和 Outcome；
  既有测试另核该 EvidenceRecord 的 ID 确实进入重新派生的 delivered。
- **G3**：终态 None 等价回归先用 MemoryIncidentStore.accept 建立 incident/Run，
  claim 后经 committer.commit_step 与 publish 完成，再执行两种 runner.resume。
  保留 same outcome/already_completed/模型工具零调用，并核验 store 业务快照不变。
- **G4**：不同静态工具反例的版本使用合法小写 v1-canary-other，确保进入
  binding 比较后才出现 IDENTITY_MISMATCH；scope-policy 反例与正向例均保留。

本轮只调整离线 fixture/断言证据，网络与子进程守卫保持原样，无真实外调。
