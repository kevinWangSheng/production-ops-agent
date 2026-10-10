# M1-04 告警接入：合同决定表

- 修订：r1 = A–D（用户 2026-10-10，见[任务记录](2026-10-10-m1-04-alert-intake.md)）；r2 = E1–E15（用户 2026-10-10 全部按推荐采纳）与 R1–R12（可逆默认，按推荐）。
- 预审：Codex（只读，2026-10-10），未参与实现。lead 复核：E5 依据属实——`accept()` 建事故必同时插 Run（`opspilot/persistence/incidents.py:38-58`），工作台快照经 `rebuild()` 读 Run（`opspilot/web/service.py:910-925`）。
- 与 r1 的张力：A「建事故不绑目标、无 Run」在现有模型下需要新的「仅交接」事故语义（E5），含迁移与页面改动；E7（多目标命中）推荐走同一路径。


范围依据：`SPEC.md` 2026-10-10 段、任务记录 r1、#167 正文、C3 §4/§6/§9/§11、PRODUCT-CONSTRAINTS、F1/F2/F7。  
#167 issue API 在本次沙箱不可访问；以下以用户提供的正文和仓库实际代码为准。未修改文件、未提交、未启动数据库、未调用模型或集群。

## 九个必答类别

### 1. 时间：待决

- 已定：Alertmanager `startsAt` 是告警字段；Alertmanager webhook v4 的格式为 RFC3339（官方文档 webhook payload，`startsAt` 字段；源码 `webhook.go:55-63,85-90`）。
- 已定：当前工具输入窗口由提交时钟生成，`otel_demo.py:752-764`；输入快照记录 `evidence_context` 和 `scope_facts`，`context.py:367-381`。
- 待决：`startsAt` 的时区统一为 UTC；精度统一到秒还是保留微秒；未来时间、缺失时间、`endsAt < startsAt` 如何处理。
- 待决：回看时长；授权框是否严格为 `[startsAt - lookback, now]`，以及 `now` 使用数据库时钟还是接收进程时钟。
- 推荐：解析后立即转 UTC，保留秒级或更高精度的规范值；拒绝无时区、无法解析和明显非法时间；窗口末端使用数据库时钟。

### 2. 计数与口径：待决

- 已定：一组 payload 逐条处理；Alertmanager 可在一个 webhook 中发送多条告警，`webhook.go:73-90`。
- 已定：Alertmanager 源码将全部 2xx 视为成功、全部 5xx 视为重试，其他状态默认不重试，`notify/util.go:189-227`；因此整组没有逐条 HTTP 确认语义。
- 已定：r1-B 只按 `(alertmanager, fingerprint:startsAt)` 判重，annotation 变化记录新值，不创建新事故。
- 待决：部分成功、部分失败时统一返回 2xx、4xx 或 5xx；推荐逐条事务提交后，只要存在持久化失败便返回 5xx，使整组可能重试，再依靠逐条幂等消除已成功项。若要返回 2xx，必须另有持久化失败 ledger。
- 待决：`startsAt` 规范化精度、零值格式及 `fingerprint` 大小写/空白规范。
- 现有 `delivery_key()` 直接使用 `state_time.isoformat()`，`opspilot/domain/intake.py:63-107`，不能直接满足新的规范化口径。
- 现有 `/intake/events` 依赖 `classify_intake_delivery()` 的完整信封比较，`opspilot/intake.py:201-224`；不得全局改成 Alertmanager 语义。

### 3. 状态转移：待决

- 已定：事故生命周期允许 `open / observing_recovery / resolved / closed`，`0002_state_checks.py:55-61`。
- 已定：r1-A 未命中目标时接收、建事故、不绑目标、直接交接、不建 Run。
- 事实缺口：`accept()` 当前必须同时写事故和 Run，`persistence/incidents.py:34-95`；`opspilot_incidents.current_run_id` 虽可为空，`0001_baseline.py:34-44`，但 `rebuild()`、页面快照和 worker 路径要求当前 Run，`web/service.py:910-925`。
- 结论：现有模型不能完整表达“无目标、无 Run、可展示、可人工交接”的事故；需要代码合同，可能需要新增事故状态/交接事件或迁移。
- 已定：ADR-0005 要求交接不发布结论，事故保持开放；`docs/adr/0005-handoff-and-deadline-terminal.md`。
- 待决：无 Run 事故在工作台的 `state`、列表排序、详情页和人工接管后的首个 Run 如何定义。

### 4. 主体与权限：待决

- 已定：事件入口必须使用 `event_token`，`web/app.py:478-485`；`AuthConfig.event_tokens` 是哈希 token 到 actor 的映射，`web/auth.py:72-89`，并产生 `channel="event_token"` 的 Principal，`web/auth.py:122-134`。
- 已定：C3 §9 要求事件接收使用独立认证 token；可为 Alertmanager 配置独立 actor。
- 待决：Alertmanager 是否使用专用 token/actor（推荐），还是复用现有事件 actor。
- 已定：工具授权按集成目标 ID，`executor.py:639-680`；不可信上下文不得改变目标、权限或预算，PRODUCT-CONSTRAINTS:32-38。
- 待决：标签匹配字段的格式、版本和冲突处理；注册表当前严格拒绝未知字段，`schema.py:210-230`。

### 5. 数据合同：待决

- 已定：`TargetIdentity` 当前字段为 `integration_id/cluster_uid/namespace/resource_uid`，另带 `workload/health_profile_id`，`web/store.py:67-90`；环境变量加载严格校验字段，`schema.py:173-237`。
- 待决：新增匹配标签结构，例如版本化 `match_labels` 对象；是否允许多个标签集合；空值和未知标签处理。
- 待决：多目标命中时拒绝、选择唯一最高优先级或建无目标事故；推荐拒绝歧义并交接，避免隐式授权。
- 已定：D 的受影响服务为 `namespace + workload` 焦点信息，不收窄工具授权。
- 待决：受影响服务放入 `InvestigationInput.scope_facts`、独立字段还是 ledger；当前输入快照只有 `question`、`bound_target_id`、`scope_facts` 等，`context.py:367-381`。
- 待决：服务焦点是否改变 `INPUT_VERSION`、tool schema revision 或 projection revision；推荐增加输入快照版本，旧 Run 按既有不兼容规则阻塞。

### 6. 旧数据与已归档证据：待决

- 已定：旧 `/intake/events` 的 `source/external_event_id` 及 composite key 行为由 `delivery_key()` 保持，`domain/intake.py:63-115`。
- 已定：旧入口的重复 key 冲突必须继续返回 `INTAKE_KEY_CONFLICT`，现有测试覆盖，`tests/test_m1_web_workbench.py:134-150`。
- 待决：Alertmanager 新 comparator 是否独立于 `classify_intake_delivery()`；推荐新增 Alertmanager 专用身份比较，不改变旧入口行为。
- 待决：旧 ledger 行是否需要回填 fingerprint、startsAt 或事件类型；推荐不回填，新增记录带 schema/version。

### 7. 并发与水位：待决

- 已定：ledger `put()` 使用数据库唯一键和 `ON CONFLICT DO NOTHING`，`web/store.py:343-358`。
- 已定：事故与 Run 创建在现有 `accept()` 事务中完成，`persistence/incidents.py:35-105`。
- 待决：同一 fingerprint:startsAt 并发首投与重放如何保证只有一个事故；推荐唯一约束或单独 AlertIdentity 表，并在同一事务锁定。
- 待决：annotation 更新是追加不可变事件、更新一行，还是 ledger 记录版本序列；推荐追加事件并保留最新值索引，避免覆盖审计证据。
- 待决：组内不同告警是否各自独立事务；推荐逐告警事务，响应汇总成功/失败计数。

### 8. 外部调用：待决

- 已定：第 2 步不应调用模型；问题模板和目标解析必须确定性完成。
- 已定：原始告警进入模型前应脱敏和限长；`context.py:924-938` 对输入内容执行 `redact_credentials` 后截断。
- 已定：首轮问题直接进入 user message，`context.py:445-464`；当前普通 `question` 没有在该处脱敏。
- 待决：Alert 原始上下文字段白名单、每字段长度、整体字节上限、脱敏顺序和是否保留 annotation key。
- 已定：工具 profile 当前授权框由输入快照恢复，`otel_demo.py:777-792`；不能从模型或 annotation 重新决定窗口。
- 待决：真实 trace 是否允许包含原始 payload；推荐只导出已脱敏、限长后的上下文摘要。

### 9. 验收投影与 `passes`：已定（范围内不翻转）

- 已定：外部验收入口为 `IncidentScenario -> IncidentOutcome`，`acceptance.py:74-117`。
- 必测：重复通知、annotation 变化、组内多告警、错误目标、resolved、超大 annotation、恶意 credential/instruction、并发首投、部分失败重试、无 Run 交接。
- 待决：`IncidentOutcome` 是否新增 `affected_service`、`alert_identity`、`raw_payload_recorded`、`per_alert_results`；推荐新增可观察字段，但保持现有字段兼容。
- 已定：F1/F2/F7 的 `passes` 不因本预审或实施计划改变；实现通过前仍保持 `false`。
- 已定：F7 第 5 步要求可重建输入、查询、证据、状态和权限；原始 payload 需有 ledger 引用和 hash。

## 需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| E1 | 部分成功如何回应 | 统一 2xx：避免重试但可能丢失败；统一 5xx：整组重试但依赖幂等；逐条结果协议：Alertmanager 不支持 | 失败任一条返回 5xx，成功条幂等 | webhook、重试、审计 || 采纳推荐（用户 2026-10-10） |
| E2 | `startsAt` 规范化 | UTC 秒；UTC 微秒；保留原字符串 | UTC 秒级规范值并保留原始值 | 去重、时间框 || 采纳推荐（用户 2026-10-10） |
| E3 | AlertManager 身份索引存储 | 新表；现有 ledger 前缀；事故表新列 | 新表或 ledger+唯一索引，需原子关联 | 迁移、并发、resolved || 采纳推荐（用户 2026-10-10） |
| E4 | 旧 `/intake/events` comparator | 全局修改；Alertmanager 专用 comparator；扩展 classify 类型 | 专用 comparator | 兼容性、旧测试 || 采纳推荐（用户 2026-10-10） |
| E5 | 无目标事故如何持久化 | 允许 null Run；新增 handoff-only 主体；拒绝入库 | 新增明确 handoff-only 事故语义 | schema、页面、人工交接 || 采纳推荐（用户 2026-10-10） |
| E6 | 标签注册表合同 | 平面标签；版本化匹配集合；独立 target-matcher 表 | 版本化 `match_labels` | 配置、迁移、部署 || 采纳推荐（用户 2026-10-10） |
| E7 | 多目标命中 | 拒绝并交接；优先级选一个；建立多绑定 | 拒绝歧义并交接 | 权限、目标绑定 || 采纳推荐（用户 2026-10-10） |
| E8 | resolved 关联规则 | 同 fingerprint+startsAt；同 fingerprint 不同 startsAt；同目标最近开放事故；找不到仅记录 | 同身份精确关联；找不到仅记录 | 生命周期、页面、审计 || 采纳推荐（用户 2026-10-10） |
| E9 | annotation 更新记录 | 覆盖最新值；追加版本事件；只 hash 不存正文 | 追加版本事件并索引最新值 | ledger、重建、保留 || 采纳推荐（用户 2026-10-10） |
| E10 | 原始 payload 限制 | 仅 hash；脱敏后限长 JSON；原文限长 | 脱敏后限长 JSON + 原始 hash | F7、trace、隐私 || 采纳推荐（用户 2026-10-10） |
| E11 | 受影响服务字段 | scope_facts；独立 input 字段；事件 metadata | `scope_facts.affected_service` 并版本化输入 | Run、页面、观察预填 || 采纳推荐（用户 2026-10-10） |
| E12 | 验收投影新增字段 | 不扩展；增加 per-alert 结果；增加服务/身份/审计引用 | 增加可观察 per-alert 结果和审计引用 | 独立测试、F1/F7 || 采纳推荐（用户 2026-10-10） |
| E13 | webhook token | 复用 actor；Alertmanager 专用 actor/token | 专用 token，配置为 `credentials_file` | 认证、审计、轮换 || 采纳推荐（用户 2026-10-10） |
| E14 | kind lab 网络路径 | Alertmanager 访问宿主 NodePort；host gateway；workbench 入集群 | 明确、可验证的 NodePort/宿主回环路径 | 实验可重复性 || 采纳推荐（用户 2026-10-10） |
| E15 | 实验 Alertmanager 版本与 chart | OTel Demo 子 chart；独立 pinned chart；镜像手工部署 | 独立 pinned Alertmanager/chart，并记录版本 | 供应链、重现 || 采纳推荐（用户 2026-10-10） |

## 可逆默认

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| R1 | webhook JSON 解析 | 只接受 object；接受 object/list | 只接受 v4 object，版本不符 400 | 入口校验 || 按推荐（可逆默认） |
| R2 | 空字段处理 | 缺失即失败；填默认值 | fingerprint、startsAt、labels 缺失即逐条失败；annotation 可空 | 失败分类 || 按推荐（可逆默认） |
| R3 | 模板字段 | 仅 alertname/summary/description/severity/startsAt/PromQL；加入全部 annotation | 仅合同字段；annotation 只进不可信上下文 | prompt 安全 || 按推荐（可逆默认） |
| R4 | 模板长度 | 固定字符上限；按 token 估算 | 固定字节/字符双上限，超限拒绝或截断并记录 | 输入预算 || 按推荐（可逆默认） |
| R5 | 多告警处理顺序 | payload 顺序；按 fingerprint 排序 | 保留 payload 顺序，事件序号单调递增 | 审计重建 || 按推荐（可逆默认） |
| R6 | 失败分类 | 400/409/422 混用；统一 `ALERT_INVALID` | 解析错误 400，目标歧义/缺失按 E5 规则 | 客户端重试 || 按推荐（可逆默认） |
| R7 | raw ledger namespace | `intake`；`alertmanager_raw`；独立表 | `alertmanager_raw` namespace，key 为规范身份+版本 | F7 重建 || 按推荐（可逆默认） |
| R8 | 脱敏实现 | 复用 `redact_credentials`；新增规则 | 复用现有函数并增加测试样本 | 安全、维护 || 按推荐（可逆默认） |
| R9 | 时钟来源 | 接收进程；数据库 `clock_timestamp()` | 数据库时钟作为提交与窗口末端 | 一致性 || 按推荐（可逆默认） |
| R10 | Alertmanager webhook 配置 | token 写 values；Secret 挂载文件；环境变量模板 | Secret 挂载文件，`authorization.credentials_file` | 凭据不入库 || 按推荐（可逆默认） |
| R11 | PromQL 规则 | 直接使用 checkout 错误率；固定录制规则 | 固定 selector、阈值、for、labels，并用 promtool 校验 | 实验稳定性 || 按推荐（可逆默认） |
| R12 | 旧 Run 兼容 | 自动迁移输入；revision 不变；阻塞旧 Run | 输入版本变化即按既有 `INCOMPATIBLE_STATE` 阻塞 | 恢复与审计 || 按推荐（可逆默认） |

未确认项：#167 GitHub API 在原工作阶段不可访问；OTel Demo 0.37.8 是否自带 Alertmanager 子 chart、最终 Alertmanager 镜像版本、webhook 从 pod 到宿主工作台的实际路由，需要实施前由实验配置和冻结证据确认。官方 Alertmanager 已确认 2xx/5xx/4xx 重试语义，见 `notify/util.go:189-227` 与 `webhook.go:121-125`；Bearer 可通过 `authorization.credentials_file` 配置，见 Prometheus Alertmanager configuration webhook `http_config` 文档。

已核对文件列表：`AGENTS.md`、`docs/tasks/README.md`、`SPEC.md`、`docs/tasks/2026-10-10-m1-04-alert-intake.md`、`PRODUCT-CONSTRAINTS.md`、`docs/design/technical-proposal-2026-09-07.md`、`docs/adr/0005-handoff-and-deadline-terminal.md`、`feature_list.json`、`opspilot/intake.py`、`opspilot/domain/intake.py`、`opspilot/web/app.py`、`opspilot/web/auth.py`、`opspilot/web/store.py`、`opspilot/schema.py`、`opspilot/persistence/incidents.py`、`opspilot/persistence/runs.py`、`opspilot/persistence/controls.py`、`opspilot/investigation/inputs.py`、`opspilot/investigation/context.py`、`opspilot/tools/executor.py`、`opspilot/tools/otel_demo.py`、`opspilot/tools/registry.py`、`opspilot/acceptance.py`、`opspilot/migrations/versions/0001_baseline.py`、`0002_state_checks.py`、`0004_target_identity.py`、`0005_incident_mode.py`、`scripts/kind_lab.py`、`scripts/kind_lab/values.yaml`、`scripts/kind_lab/kind-config.yaml`、`docs/development.md`、`opspilot/observer/profiles/otel-demo-checkout.json`、`tests/test_m1_web_workbench.py`、`tests/test_m1_intake_auth.py`、`tests/test_m1_otel_demo_contract.py`、`tests/test_m1_tool_boundaries.py`、`tests/test_m1_investigation_context.py`。

## 第 3、4 步（预审 Codex 只读，2026-10-10；裁决 r5）

结论：第 3、4 步均可实施，但以下合同点尚未由 r2 或现有代码决定，不能直接开工。当前工作树已有用户改动 `docs/tasks/2026-09-27-m1-01-report-contract.md` 与未跟踪 `.playwright-mcp/`，本次未修改。

### 九类必答

1. **时间：待决（第 3 步）**

   已定：当前 OTel Demo 授权框为 24 小时，`OBSERVATION_SECONDS = 24*3600`（`opspilot/tools/otel_demo.py:202,752-764`）；入口注入 `DurableClock`，其 `now()` 使用数据库 `clock_timestamp()`（`opspilot/web/__main__.py:136-149`、`opspilot/web/store.py:394-406`）。  
   待决：回看值是否固定 24 小时；`startsAt` 未来、缺失、非法、超过 24 小时如何处理；`startsAt` 是否规范到 UTC 秒级。  
   非告警入口保持现状：`/intake/events` 的 comparator 不应改变（合同表 r2、`opspilot/domain/intake.py:63-115`）；`/intake/ui` 继续由提交时钟建窗。

2. **计数与口径：待决（第 3、4 步）**

   已定：每 Run 一个反循环上限；当前默认模型请求上限 100（`opspilot/investigation/limits.py:22-34`），模型请求超时 360 秒、Run backstop 7200 秒（同文件 `:87-93`）；工具请求上限 30 秒、结果 2 MiB（`opspilot/tools/registry.py:64-65`）。  
   待决：回看是否始终 24 小时，还是允许告警字段指定但受 24 小时上限约束；原始告警字段和消息限长采用字符、字节或双上限。

3. **状态转移：待决（第 3、4 步）**

   已定：授权窗口从输入快照读取，不能由模型或告警文本重新决定（`opspilot/tools/otel_demo.py:777-792`）；超时续开正常沿用前一 Run 的 `time_policies`（`tests/test_m1_investigation_continuation.py:73-88`）。  
   已定：只有旧 Run 缺少输入快照时才 fresh input；其他无法继续的 `ContextError` 拒绝控制动作（`opspilot/web/service.py:573-593`）。  
   待决：续开 fresh input 时是否必须沿用原告警 `startsAt`；`startsAt` 早于授权上限时是拒绝、截断还是交接。

4. **主体与权限：已定**

   告警使用 `event_token` Principal（`opspilot/web/auth.py:122-134`）；工具目标授权仍由注册表和 Run scope 控制（`opspilot/tools/executor.py:639-678`）。原始告警不得改变目标、时间框、预算或权限（`PRODUCT-CONSTRAINTS.md:26-38`）。受影响服务仍仅为 `scope_facts.affected_service` 焦点信息，不能缩小工具授权（任务记录 r2 E11、`opspilot/investigation/context.py:326-381`）。

5. **数据合同：待决（第 3、4 步）**

   已定：锚点应进入输入快照；当前快照字段为 `evidence_context`、`scope_facts` 等（`opspilot/investigation/context.py:326-381`）。  
   已定：工具 schema revision 由内容哈希生成（`opspilot/tools/otel_demo.py:630-647`）；调查 loop 版本含 prompt 与 context policy revision（`opspilot/investigation/loop.py:140-147`）。  
   待决：锚点字段具体名称和结构；是否递增 `INPUT_VERSION`、tool schema revision、projection revision。`PROJECTION_REVISION` 当前为 `m1-01-tool-view-v7`（`opspilot/tools/outcomes.py:52-61`），与输入锚点不是同一层。

6. **旧数据与已归档证据：待决（第 3 步）**

   当前 `InvestigationInput.from_json()` 只接受精确 `INPUT_VERSION`，失败为 `INPUT_INVALID`（`opspilot/investigation/context.py:383-411`）；claim 阶段版本字典不一致才标记 `INCOMPATIBLE_STATE`（`opspilot/persistence/runs.py:175-211`）。  
   因此“递增 INPUT_VERSION 后旧 Run 按既有 `INCOMPATIBLE_STATE` 阻塞”目前不会自动成立，需明确迁移或 claim 映射。  
   推荐保持旧 Run 不猜测迁移；不能解析的旧 Run 显式阻塞并交接。无锚点旧 Run 的具体恢复行为仍待决。

7. **并发与水位：待决（第 3、4 步）**

   已定：每次尝试从 Run 自己的快照建 executor；目标 registry 或 tool registry revision 变化会拒绝（`opspilot/investigation/runner.py:241-267`、`opspilot/tools/executor.py:643-647`）。  
   待决：`startsAt` 锚点与输入快照写入是否必须和 Run 创建同一事务；第 4 步原始告警是否在每轮重复注入。  
   推荐只在首轮固定前缀注入，压缩时保留该前缀；不要让每轮重复扩大上下文。

8. **外部调用：待决（第 4 步）**

   已定：原始告警必须先 `redact_credentials` 再截断（任务记录 r2 E10、`opspilot/investigation/context.py:916-937`）；现有规则是每字段最多 8192 字符，先脱敏后截断。  
   已定：当前 trace 只在 synthetic lab 导出，字段经过 allowlist，凭据和 provider 私有字段被丢弃（`opspilot/tracing.py:235-334`；ADR-0006）。  
   待决：原始告警消息角色、位置、字段白名单、整体字节上限、是否保留 annotation key；注入文本的防护措辞若改变模型可见模板，必须 bump `prompt_revision`。  
   当前首轮 `question` 直接作为 user 消息，未脱敏（`opspilot/investigation/context.py:445-464`）；这是独立缺陷，应另开 PR，不与原始告警上下文合同混在同一 PR。

9. **验收投影与 `passes`：已定范围，验收细节待决**

   外部入口是 `IncidentScenario -> IncidentOutcome`（`opspilot/acceptance.py:74-117`）；本切片不翻 F1/F2/F7 的 `passes`（任务记录 r2、`feature_list.json:1-15`）。  
   确定性断言：窗口边界、UTC 规范化、未来/过旧处理、输入快照字段、旧入口兼容、消息角色/字段/限长/脱敏顺序、压缩保留、版本不兼容状态。  
   真实 Run 才能证明：实际模型收到的消息、工具授权仍受锚定窗口约束、上下文压缩后仍保留告警、LangSmith trace 与 `summary.json` 可复核。每步至少一条有界真实 Run；摘要需含 trace ID/链接、版本、脱敏输入摘要、结果和 ledger hash，不提交原始 ledger（ADR-0006:14-20）。

### 需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| F1（第3步） | 回看默认值与上限 | 固定 24h；可配置但上限 24h；沿用模型查询窗。固定最简单；可配置增加合同面 | 固定 24h 授权框，模型只能在框内选窄窗 | 输入、工具授权、验收 || 采纳推荐（用户 2026-10-10） |
| F2（第3步） | `startsAt` 未来或超过 24h | 拒绝该告警；截断到 `[now-24h, now]`；接收但交接无 Run。拒绝最明确，截断会改变告警语义 | 未来/非法拒绝；过旧截断并记录原值，或统一交接（需选定） | intake、状态、审计 || 采纳推荐（用户 2026-10-10） |
| F3（第3步） | 时间精度与末端 | UTC 秒；UTC 微秒；数据库时钟或接收进程时钟 | UTC 秒；数据库 `clock_timestamp()` | 去重、窗口、重放 || 采纳推荐（用户 2026-10-10） |
| F4（第3步） | 锚点版本兼容 | 只递增 `INPUT_VERSION`；加入 versions 比对；显式迁移旧快照。前者当前会报 `INPUT_INVALID` | 新字段 + versions 变更，旧 Run `INCOMPATIBLE_STATE` 阻塞 | 恢复、部署顺序、审计 || 采纳推荐（用户 2026-10-10） |
| F5（第3步） | 续开与锚点 | 始终沿用前一 Run；fresh fallback 重新取窗；fresh fallback 沿用告警 `startsAt` | 沿用告警 `startsAt`，不得重新按提交时刻取窗 | 超时续开、输入重建 || 采纳推荐（用户 2026-10-10） |
| F6（第4步） | 不可信消息角色与位置 | `user` 首轮固定消息；system 附注；每轮重复。system 会改变提示层，重复会放大预算 | 独立 `user` 消息，紧随 question，仅首轮固定前缀 | prompt、压缩、预算 || 采纳推荐（用户 2026-10-10） |
| F7（第4步） | 字段白名单与限长 | 仅 labels/annotations；加 alertname/summary 等结构字段；按字段或整体限长 | 结构字段进入确定性模板；labels/annotations 单独 allowlist，先脱敏后双上限截断 | 安全、上下文预算、trace || 采纳推荐（用户 2026-10-10） |
| F8（第4步） | 防注入措辞是否 bump prompt | 不加措辞；加入固定数据边界措辞。加入会改变 prompt bytes | 加入固定措辞并 bump `prompt_revision` | 旧 Run、trace、验收 || 采纳推荐（用户 2026-10-10） |
| F9（第4步） | 首轮 question 脱敏归属 | 本 PR 修；单独缺陷 PR。混做会违反“一 PR 一个合同条款/明确缺陷” | 单独缺陷 PR，现 issue 保持原始告警合同范围 | 安全、审查、PR 边界 || 采纳推荐（用户 2026-10-10） |
| F10（第3、4步） | 真实 Run 证据门槛 | 两步合并后一次 Run；每步各一次 Run。后者证据归属清晰但成本更高 | 每步至少一次独立有界 Run；每 Run 一个反循环上限 | LangSmith、summary、费用 || 采纳推荐（用户 2026-10-10） |

### 可逆默认

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| S1（第3步） | `ToolFace` 接口形状 | 可选 `starts_at`/`lookback` 参数；新增锚点对象；保留兼容默认 | 新增可选锚点参数，旧调用按现有提交时钟，便于旧 Run 兼容 | `inputs.py`、`otel_demo.py` || 按推荐（可逆默认） |
| S2（第3步） | 输入字段名 | `scope_facts.alert_starts_at`；顶层字段；嵌入 time policy | `scope_facts.alert_starts_at` + time policy 的实际窗口，最少扩展 | 快照、重建、投影 || 按推荐（可逆默认） |
| S3（第3步） | 旧入口行为 | 改共享 helper；告警专用 helper | 告警专用 helper；`/intake/ui`、`/intake/events` 不变 | 兼容测试 || 按推荐（可逆默认） |
| S4（第4步） | 原始告警编码 | canonical JSON；逐字段文本；两者并存 | canonical JSON，固定 key 顺序，便于 hash、重建和限长 | prompt、trace、审计 || 按推荐（可逆默认） |
| S5（第4步） | annotation key 处理 | 保留 key；仅保留值；正则过滤 key | 保留 key，值先脱敏并限长；key 也纳入长度预算 | 注入防护、可读性 || 按推荐（可逆默认） |
| S6（第4步） | 压缩策略 | 保留完整消息；摘要替换；每轮重新注入 | 作为固定前缀保留；压缩只替换前缀之后的历史，符合 `context.py:743-746` | 恢复、prompt revision || 按推荐（可逆默认） |
| S7（第3、4步） | 实施顺序 | 并行；先第3后第4 | 先第3步，再第4步。两步都会触及输入/消息版本与真实 Run 证据，顺序可避免冲突 | worktree、revision、测试 || 按推荐（可逆默认） |
| S8（第3、4步） | trace 内容 | 原始 payload；脱敏限长上下文；只摘要 | 仅导出脱敏限长上下文和摘要，保留原始 hash | ADR-0006、隐私、审查 || 按推荐（可逆默认） |

已核对文件：`AGENTS.md`、`docs/tasks/README.md`、`docs/tasks/2026-10-10-m1-04-alert-intake.md`、`docs/tasks/2026-10-10-m1-04-alert-intake-contract.md`、`SPEC.md`、`PRODUCT-CONSTRAINTS.md`、`docs/design/technical-proposal-2026-09-07.md`、`docs/adr/0006-trace-evidence-and-backlog.md`、`ROADMAP.md`、`opspilot/investigation/inputs.py`、`opspilot/investigation/context.py`、`opspilot/investigation/loop.py`、`opspilot/investigation/runner.py`、`opspilot/tools/otel_demo.py`、`opspilot/tools/executor.py`、`opspilot/tools/outcomes.py`、`opspilot/tools/registry.py`、`opspilot/tools/profiles.py`、`opspilot/tracing.py`、`opspilot/web/service.py`、`opspilot/web/store.py`、`opspilot/web/__main__.py`、`opspilot/web/auth.py`、`opspilot/persistence/runs.py`、相关 `tests/`。
