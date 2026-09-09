# PR 审查门禁

目标：开发者提交后，GitHub 检查最新提交的机器人审查是否完成、讨论是否处置；通过后由用户最终审核合并。此为开发流程，不授予 OpsPilot 产品发布门禁或执行权限。

## 执行与证据

[工作流](../.github/workflows/review-gate.yml)只运行main中的[标准库脚本](../scripts/review_gate.py)，不执行PR代码、不获取业务Secrets、不调用模型、不自动合并。

脚本读取全部分页的PR评论及review threads，只接受API身份为Bot/199175422的Codex汇总；完整headSha必须等于当前HEAD，Code Review与Security Review两项均为Completed，所有讨论必须resolved（含outdated），汇总完成后的新审查请求仍阻塞。不接受仅点赞、无评论、旧SHA、单独Code Review结果或模型自行声称完成。供应商格式未知时拒绝通过；无稳定官方JSON合同，适配器依赖已捕获的实际汇总格式，变更时需修复及复验。

运行 `python3 scripts/review_gate.py --pr <编号>` 只读判定；退出0才是就绪，其他结果列明原因。没有参数时检查全部开放main PR。

workflow在PR打开/推送等事件、issue评论变更和手动触发时重算，并以5分钟cron补偿。cron可能延迟，不能承诺固定处理时间；线程重开即时合并阻断由原生讨论解决规则负责。全局串行，复用同SHA的单个check-run（避免commit status历史上限），先in_progress再查询，成功前重读证据；同SHA多PR拒绝成功。API或发布失败必须检查运行日志，不能把旧成功视作新证据。

2026-09-09本仓库Codex设置已从On PR open改为On every push，Review all PRs保持启用，未改个人默认或开启credits。配置已回读；每次push的实际完成需以真实PR验证。官方[Code Review说明](https://learn.chatgpt.com/docs/third-party/github)支持自动审查，但规则文字不能替代分支保护。

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
