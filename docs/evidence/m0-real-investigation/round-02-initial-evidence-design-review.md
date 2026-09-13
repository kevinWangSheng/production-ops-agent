# 初始持久证据与版本绑定独立设计审查

基线PR16 `6eab0a4b139a130afde98f4105c39e1d15c145a8`，2026-09-10。接续独立v4审查上下文，未参与实现。通过GitHub API亲读3976875093/5099/5104原发现，并读取当前store、bridge与runner的实际输入/配置路径。新发现说明上轮有界正例未覆盖完整report-only桥接；旧审查记录保留，不追改为当时已经验证。

## 确认的问题

- `StepStore.control`推进cancel/correct generation/state却保留当前final；历史report记录已有独立保留，清除当前指针符合人控优先。该小修由另一独立验证者检查实际存储生命周期，本审查不以静态读取替代PG证明。
- 一请求report-only输入的`business_tool_views`在runner登记，但bridge无observations文件直接读取失败；即使文件为空，`extract_registered_views`跳过user，且scenario初始view恒空。初始输入和实际最后物理请求缺完整业务重建。问题不能只靠创建空observations.json解决。
- `versions`缺已配置的upstream_commit/tool_schema。应绑定执行配置的实际字段；缺字段只能unknown，不能以当前仓库版本或文件名猜测当时运行身份。

## 关键合同约束

1. 初始证据必须有真实raw、原manifest、原投影view/version、源代码hash及适用registry/context，验证原始与投影hash及实际派生关系。不能把裁剪后的view包装为raw，不能用新算法重签旧视图，也不能从问题文本或复审时钟补capture。
2. user-message的业务证据只按明确顶层`business_tool_views`入口和完整登记记录解析，并与真实物理请求business消息/hash核对；不在任意嵌套文本搜evidence_id。源记录存在和实际送模是两个条件。
3. 合法一请求报告必须能形成`IncidentScenario -> IncidentOutcome`严格检查。metadata缺失、混合context不支持时仍保留真实输入、完整报告和具体unknown原因，不能变FileNotFound、静默legacy或虚造可通过的证据。
4. 当前单一全局ProjectionContext不能无条件解释不同旧投影。最小可接受方案可先支持明确同context的bundle，异context显式不支持/unknown；若采用per-artifact context扩展，需严格版本/源码/registry绑定并复验所有初始、动态和混合引用路径，不引入一般平台。
5. 保持v4当前候选、旧v1/v3历史schema与原始报告边界；版本需纳入实际upstream_commit和tool schema hash。初始输入、原报告仍按原授权本地保存，tracked只元数据/hash。

当前结论：三项原发现成立；具体候选方案尚待作者提交，关键schema/接口改动尚未审查通过。

## 具体最小方案复核

已直接取得合同作者与runtime作者的8点方案：

1. 人控更新同一事务清空当前`final`，原历史report保留；另由独立存储验证者复验。
2. report-only且max_steps=1时，无observations表示零动态查询；新runner始终初始化空记录。
3. 显式可信CLI initial manifest含原raw/view/manifest的exact bytes/hash、原投影revision/source/dependencies/registry context与可选独立Timing。验证重放后复制到新Run，原数据不移动、不改写；导入不冒充本Run查询。
4. 与实际user顶层business_tool_views逐项核对，已验证初始view进入AgentInput.initial_views；最后物理请求仍需实际包含对应view/context。
5. 只支持单一兼容原ProjectionContext。原registry、source及dependencies/version必须一致；混合context明确unknown并保全输入/报告，不扩per-artifact平台。
6. 增加小型UnverifiedInitialView审计记录（content/hash/reason）及原始user业务内容/hash，缺失、无效或不兼容来源仍可形成含完整报告的strict Outcome，checker返回UNVERIFIED_INITIAL_EVIDENCE；investigator_input拒绝未验证证据，绝不伪造Artifact。
7. versions绑定config的upstream_commit与canonical tool_schema hash；缺失需明确strict unknown/违规，不能让unknown字符串因两边相等而通过。
8. 合同作者处理store/bridge/schema，runtime作者处理初始manifest验证/字节复制/实际输入记录，环境与付费调用仍为零。

独立要求明确以下兼容细节：保留当前已提交v4 schema为旧快照，新增审计字段不改变旧记录的历史含义；manifest自动装入视图时，原question content/hash与转换后的actual user content/hash分开保存，checker绑定actual physical payload；坏/重复ID的输入也保留原内容与定位/原因，不能因UnverifiedInitialView构造失败丢失报告。未知记录的审计定位不是新证据身份，不能进入可引用target/evidence目录。

上述具体方案及细节属于原合同缺口修复，**有界设计通过，可进行离线实现**。通过要求实现遵守上述记录保真和版本边界，不批准扩大projection平台、外部数据用途、费用、PG/环境操作或新的模型请求。完成后仍须实际单请求report-only穿过最高接缝、metadata缺失与混合context保全、错误/未交付初始证据拒绝、版本变化分离和旧schema回归的独立复验；设计通过不是运行或产品验收通过。

## 初始路径活动实现预验

亲跑`initial_report_probe.py`（固定Holmes、替换pipe/dotenv）成功：1个假模型请求、0工具、0真实HTTP；真实runner输出经过load_packet到strict checker，无违规，初始view=1。另以独立临时fixture将raw/view/manifest/source/question换成CRLF并重新登记真实字节hash，importer逐件复制字节完全相等，未改原文件。

发现待修边界：runtime在schema合法但context资格校验失败时只保存原文/capture，不保存`result.final_report`；bridge仍无条件要求该解析副本存在。以真实形状fixture保留完整原文/capture、移除final_report并记资格失败，load_packet实际抛`REPORT_PARSED_OBJECT_MISMATCH`，未进入strict Outcome。缺metadata但模型仍给有结构的fact会触发；应分离原文解析候选与资格结论，不能只拿无claims报告测unknown。已向两位作者及root同步，尚未关闭。
