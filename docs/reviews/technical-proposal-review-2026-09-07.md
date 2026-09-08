# 首版技术方案独立一致性审查

历史记录：本文审查的是 C3 之前的提案。旧 checkpoint 恢复方案和当时待批准状态已由 [C3 全文审查](technical-design-c3-review-2026-09-07.md) 与当前技术方案替代；下文保留当时结论，不作为当前协议。

日期：2026-09-07。对象：[技术提案](../design/technical-proposal-2026-09-07.md)。独立审查者：`state_design_review` subagent。文档级审查，未执行代码或运行验证。

初审认为整体符合只读与无症状路由边界，checkpoint epoch隔离被明确标为M0否决项，没有假称框架已提供完整保证。发现四处应补严：

1. 同一Incident的两个正常Run可能以相同control_generation覆盖彼此，只有Run租约不够。
2. 证据保留期没有覆盖checkpoint/provider history/outbox等副本，知识可能留下悬空引用。
3. 模型响应已持久、checkpoint尚未成功时，重放新模型响应可能产生不同tool ID，逃过operation去重。
4. 并行请求与重启后预算预留、未知用量和累计规则不明确。

修订后的对应规则：

- §4 限制一个当前调查结果Run，使用current_run_id保护；恢复观察独立通道。
- §11 pin活动/恢复/审核知识/归档评测依赖，统一副本保留并显式tombstone。
- §7 稳定ModelStep，先复用已提交完整assistant结果，再派生工具operation ID。
- §13 数据库原子预算预留、未知消耗保守记账、新epoch不重置预算。
- 额外在§12区分进程/网络故障恢复与整机磁盘毁坏的RPO，避免零丢失范围含糊。

独立复查确认上述四项在提案层已充分处理，可以提交用户审核。具体实现保证仍需M0及后续故障验证；不代表技术方案已被用户批准，也不修改原验收。
