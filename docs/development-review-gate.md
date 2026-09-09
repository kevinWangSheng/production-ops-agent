# PR 审查门禁

**当前为未完成草案，不得激活：#13最新机器人复审提出旧success在pending API失败时仍可放行的P1。见[最新任务阻断](tasks/2026-09-09-pr-review-gate.md#最新阻断不得激活或视为就绪)。以下为受检实现及候选部署步骤，不代表已满足强制闭环。**

目标：开发者提交后，GitHub 检查最新提交的机器人审查是否完成、讨论是否处置；通过后由用户最终审核合并。此为开发流程，不授予 OpsPilot 产品发布门禁或执行权限。

## 执行与证据

[工作流](../.github/workflows/review-gate.yml)只运行main中的[标准库脚本](../scripts/review_gate.py)，不执行PR代码、不获取业务Secrets、不调用模型、不自动合并。

脚本读取全部分页的PR评论、正式reviews及review threads，只接受API身份为Bot/199175422的结果，所有讨论必须resolved（含outdated）。有两种已观察的证据入口：

- 存在Codex汇总特征时，只接受严格已知格式、完整headSha等于HEAD、Code Review与Security Review两项Completed。损坏或旧汇总不能退回宽松入口。
- 没有汇总特征的Code Review单独安装：最新真实bot正式review必须带API完整commit_id等于HEAD、标准Codex Review正文头、已提交的COMMENTED/APPROVED状态；较新pending/dismissed不能回退历史成功。该入口只证明Code Review，不声称Security Review通过。

只将GitHub认证OWNER/MEMBER/COLLABORATOR的请求计入等待；请求创建/修改时间不早于完成时间时阻塞。Code-only入口的显式Security请求无完成汇总时阻塞，任何仍存的受信定向请求均阻塞，防止其迟到结果被后来的全量请求冒用；缺少可验证触发关联时不能假定结果属于后一次请求。当前个人仓库适用；迁移组织后MEMBER并不代表仓库写权限，需重新审查授权映射。

**已知可用性限制：清洁审查可能只给👍，没有绑定完整SHA的正式review或汇总。此时返回NO_COMMIT_BOUND_REVIEW，不能自动通过。** 已捕获正式COMMENTED只证明机器人提交了标准审查，不能证明其阅读覆盖率或诊断正确性；最终人审仍必要。供应商格式未知时拒绝通过，适配器变化需修复及复验。

运行 `python3 scripts/review_gate.py --pr <编号>` 只读判定；退出0才是就绪，其他结果列明原因。没有参数时检查全部开放main PR。

workflow在PR打开/推送等事件、issue评论变更和手动触发时重算，并以5分钟cron补偿。cron可能延迟，不能承诺固定处理时间；线程重开即时合并阻断由原生讨论解决规则负责。全局串行，复用同SHA的单个check-run（避免commit status历史上限），先in_progress再查询，成功前重读证据；同SHA多PR拒绝成功。API或发布失败必须检查运行日志，不能把旧成功视作新证据。

2026-09-09两次尝试将本仓库Codex设置从On PR open改为On every push，界面立即显示成功，但重新加载后均恢复On PR open，原因未确认。故每次push自动复审未持久化；Review all PRs保持启用，未改个人默认或开启credits。#13初始自动正式Code Review已返回，后续通过@codex review手动请求。脚本只检查审查证据，不自动调用机器人；自动触发缺口未解决前，不能宣称全自动闭环。当前账号UI为Plus，没有SecurityReview配置项；[官方范围](https://learn.chatgpt.com/docs/security/security-review)明确Plus不提供。旧PR双汇总不能当作本账号当前可用性的证明，没有升级或新增费用。官方[Code Review说明](https://learn.chatgpt.com/docs/third-party/github)支持自动审查，但规则文字不能替代分支保护。

## 强制范围与局限

[目标配置](../.github/required-checks.json)要求GitHub Actions app的checks、m0-postgres、review-gate；保留strict、enforce_admins和required_conversation_resolution。

该机制防止正常开发遗漏审查，不隔离有仓库写权限者蓄意修改其他workflow发布同名状态。GitHub Actions app_id不代表单一workflow身份；管理员也能修改保护设置。本项目未新增独立GitHub App或凭据。[GitHub权限说明](https://docs.github.com/en/actions/concepts/security/compromised-runners)。写权限workflow只checkout main，符合[受信执行要求](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target)。

GitHub元数据读取与check发布不是原子事务。先写pending失败时旧status可能仍保留；评论在最终读取后改变也存在事件处理延迟，原生讨论保护必须一直开启。不支持merge queue、其他base分支或合并后部署。人工处置讨论表示发现已核查，不是确定性证明代码正确。

当前已实际强制checks及m0-postgres；review-gate待以下首次启用步骤完成后才强制。

## 首次启用及恢复

本PR尚未合并时，新增main工作流不会执行；不得为了bootstrap伪造success或提前把缺失的review-gate设为必需而锁死首次合并。

1. 本PR的测试、独立审查、最新CI及机器人审查通过后，由用户审核合并。
2. 核对main包含审核后的workflow和脚本，运行 `gh workflow run review-gate.yml --ref main`。用真实PR验证缺审/旧审/未解决时失败，完成复审且讨论已处理时成功；核对status的实际发布身份、HEAD及运行日志。
3. 回读main保护，保留现有必需检查并追加目标配置中的检查（不能覆盖并发新增的保护）。确认工作流已验证后，使用required_status_checks子资源PATCH，仅修改检查列表与strict；其他保护必须回读保持。激活记录须写任务文件，不能只留ignored日志。
4. 本次不自动合并任何PR。新门禁发布后的首个真实阻断与恢复记录到[任务](tasks/2026-09-09-pr-review-gate.md)。

机器人额度/服务不可用或汇总格式改变时保留阻塞，恢复机器人或审查适配器后重新执行；不自动删required check。需要应急放宽保护须由用户明确决定并留审计，不作为脚本回退。
