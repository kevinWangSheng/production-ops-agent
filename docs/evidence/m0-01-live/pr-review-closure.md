# PR #12：审查闭环与模型批准合同修复

当前状态（2026-09-09现场核对）：PR #12 已合并到 main `55539bc`；PR #13 已关闭，自动审查门禁暂缓。下文保留合并前审查及当时安全复审未取得结果的历史，不再表示 PR 待合并，也不补造安全复审结果。新的 Flash 运行见 [合同](flash-contract.md)。

本页按提交保留审查过程；其中Pro配置为当时历史。用户随后指定[默认Flash](flash-default.md)，当前配置以SPEC及MODEL_PROFILE为准，旧批准不迁移。

2026-09-09。用户指出已知code_review应等待结果并处置后才算PR交付完成；在AGENTS“变更、Git与交接”增补统一原则。已触发的Code/Security Review均须返回、发现须处置，核对覆盖提交，实质变更取得复审。只有CI与适用审查闭环才能报告“PR已就绪，待用户审核合并”，不自动获得合并授权。旧提交review/local review/CI三者不能互相冒充。无法取得审查结果必须明确未完成，不先宣告交付。

## 本次发现与处理

实际GitHub回读：Code Review和Security Review均完成，但summary覆盖3309daf，现实现已多次改变。Code Review有P1（review comment3966807552）：批准固定版本时合同没有model/version字段，仍用可变alias；实际返回不同版本也可继续请求/成功trace。此前仅报告CI成功，没有主动等待并处置该已触发审查，是交付遗漏。

修复使用m0-normal-1-v2批准合同：model_profile明确request_model、accepted_response_model、thinking、reasoning_effort及version_scope，并随完整合同持久hash绑定。当前仅支持明确批准reported_alias=deepseek-v4-pro的兼容试验；官方alias无法证明固定后端权重，所以fixed_weights或固定0813的批准一律在claim/网络前拒绝，不冒充固定版支持。每个模型响应的model必须与获批报告名完全匹配，缺失/未知/换模型立即失败；首轮不执行工具或第二次请求，第二轮也不能产生成功trace。结果用固定LIVE_MODEL_PROFILE_MISMATCH解释，不导出任意provider正文。

这不证明provider不会在同一个报告别名后更换权重，也不放开未来路由变化的授权。若用户要求不可变模型版本，须先取得真实可用的不可变地址/版本证明，再另行设计验证，当前入口明确不支持。原v1批准文件与首次实验历史保留，不迁移、不重新授权或重跑。

验证涵盖缺失/错误profile、固定权重请求、首/次轮模型不匹配与缺失返回名。独立审查见[profile-review.md](profile-review.md)。本轮无真实模型/trace或新增费用；原PR保持OPEN，后续提交后等待最新CI和机器人复审结果，不将本地通过替代远程审查完成。

## 第二轮远程审查：实际运行依赖

最新Code Review于11:35UTC返回，覆盖d14d09c，新增P2（comment3967879107）：uv.lock hash只固定意图，没有验证已安装环境。新增runtime.py，在claim前核对批准的CPython完整版本/实现、锁的Python范围以及默认dev/m0依赖闭包的实际distribution版本；按当前平台marker与extras取有效依赖，缺包/版本漂移统一拒绝。runtime字段也进入批准合同hash；没有自动安装/升级或真实调用。当前单版本锁之外的多版本解析明确拒绝，版本元数据核对不是二进制完整性证明。

固定Runtime/包漂移回归及[独立审查](runtime-review.md)完成后，再请求覆盖本次改动的远程复审，不能在新P2尚未处置时把上一轮CI/审查标为已就绪。

## 第三轮远程审查：失败分类

Code Review于11:50UTC返回，覆盖c8a8ced，新增P2（comment3968000660）：账号/存储/超时或取消被统一写为LIVE_PROTOCOL_FAILED。新增固定failure_code分类并用于business/trace异常，401/403和429单独分类，保留受控的身份、期限、存储、模型错配等错误；未知异常固定OPERATION_FAILED，不输出任何异常正文。完整路径和秘密哨兵回归覆盖认证失败、账户错配、超时、取消、存储、期限和未知异常；[独立复验](failure-code-review.md)单独记录。此为诊断准确性修复，不增加重试、出站权限或真实调用。

## Flash提交复审：异常响应边界与计划同步

12:07UTC的Code Review覆盖c0580b7，指出两个P2：comment3968131327要求畸形模型响应在解析边界分类为协议失败；comment3968131337要求当前资源计划同步Flash。后者已在等待期间本地发现并修正，随本次提交发布。前者补完整结构校验与最终JSON解析错误转换；保留profile错配阻断、未知费用占用和异常正文过滤。新增9个完整链路畸形响应回归，独立结果见[parser-review.md](parser-review.md)。只处理返回的具体发现，没有新增真实调用。

## 审计持久化与项目响应复审

12:20UTC的review覆盖70850a2，提出comment3968243058/3968243067/3968243074：安全错误码需要随状态持久化、项目2xx非对象需要受控分类、资源计划其他当前状态仍有过期文字。新增独立m0_live_diagnostics表，与业务/outbox或trace状态在同一事务写固定code；原实验行不增加或改写字段，原记录没有诊断行表示历史未分类。数据库不可用时仍不能假称已持久化，输出固定失败并保留原预算占用，不重发模型。项目非对象在任何模型调用前返回LIVE_PROJECT_RESPONSE_INVALID。资源计划全文同步现状/历史边界。

新增表只由显式本地实验setup安装，live不会迁移数据库；测试使用现有专属55431及随机新身份，原真实实验行前后hash核对。独立验证见[durable-diagnostics-review.md](durable-diagnostics-review.md)。这不是产品数据库迁移或新的真实调用授权。

本轮补充验证：make check254passed/16显式PG默认skip；新增/现有live PG专项3passed，独立67项组合测试及诊断SQL失败事务回滚通过。原真实实验行to_jsonb的SHA256前后相同。专属PG已stop，保留旧数据和新增诊断表；模型/上传新增0。

补充前提防护：claim前检查诊断表字段可查询，旧schema缺表时拒绝且不消耗授权；真实PG专项4passed，独立复验通过。最后完整检查254passed/17显式PG默认skip。专属PG再次stop，仍无模型/上传新增。

## 完成状态须以已确认提交为准

12:46UTC的review覆盖de99c41，comment3968481848指出终态保存失败仍可能返回completed。增加提交确认标记：save成功返回后才保留终态完成；失败/提交不明对外handoff并报告存储失败，已确认提交后的trace故障不覆盖业务completed。原数据库行可能处于提交结果不明状态，handoff不声称数据库一定未提交。66项离线回归及独立复验通过，无PG或真实请求新增。

## 代码审查返回后的文档收尾

12:59UTC的Code Review覆盖1822bde，仅新增开发指南旧live状态文字P2（comment3968639268）。已全文修正当前v2入口、原Pro历史/Flash未新执行、CI/PG范围；独立审查者只读核对全文无矛盾。此后仅文档修改，实现源码/测试/锁与已审1822bde一致，不把文档收尾当新的实现变更或重复重跑付费实验。

至此已返回的Code Review发现1项P1、9项P2均已处理并复验；最新实现CI成功，最终文档CI在PR实时核查。Security Review的旧3309daf结果不能覆盖新实现；两次官方手动命令未观察到接收确认，UI配置显示随Code Review运行但只有旧报告，当前安全复审仍未取得新结果。PR保持待审，不宣告整体完成或自动合并。
