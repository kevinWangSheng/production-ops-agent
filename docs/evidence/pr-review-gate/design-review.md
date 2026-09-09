# 独立设计审查与处置

日期：2026-09-09。全新上下文审查者 `/root/gate_design_review`；完整阅读 AGENTS/SPEC/ROADMAP/CI 并核对官方文档。此为设计证据，不是工作流运行证明。

- P1：Actions app 绑定不阻止同仓库写作者修改 workflow 并伪造同名 status。处置：明确门禁信任仓库写作者和管理员，防止正常开发漏等/旧版本放行，不宣称抵御有写权限的恶意作者。没有为此引入新的 App、key 或外部服务；如未来需该安全边界，必须独立发布身份或受信 required workflow 能力。
- P1：API失败/事件漏触发不会自动撤销旧 success。处置：每次评价先写 pending；查询异常写 error。若连pending写入失败则无法保证撤销，明确记录局限。schedule仅补偿，不承诺5分钟固定时效。
- P1：讨论重开不能靠 issue_comment 实时覆盖。处置：保留 GitHub 原生 required_conversation_resolution；定时重算只更新状态。不采用执行 PR 版本 workflow 的 pull_request_review / pull_request_review_comment 特权触发。
- P2：同HEAD多个PR共享status。处置：同SHA指向main的开放PR不唯一时拒绝通过。
- P2：乱序和HEAD变化。处置：全局串行、cancel=false，成功前重读完整快照及共享SHA，旧证据仅能写原SHA。API多读不能提供原子事务；保护和管理员边界必须明确。
- 引导：用户审核合并本PR后才运行受信main版本，验证真实check发布后再追加required review-gate；不伪造bootstrap通过，不预报上线成功。

来源：[Actions权限](https://docs.github.com/en/actions/concepts/security/compromised-runners)、[事件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)、[保护API](https://docs.github.com/en/rest/branches/branch-protection)。
