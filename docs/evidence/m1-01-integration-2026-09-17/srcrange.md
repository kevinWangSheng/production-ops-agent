# PR #20 source 区间与注册期秘密来源约束报告

## 结果

已在 `feature/m1-01-tool-executor` 完成并推送提交 `472a4e1`，更新 PR #20。

- `TransportResponse` 新增 `source_start_at` / `source_end_at`。两端都缺失表示来源实际覆盖区间未知；不能由请求窗口、`data_as_of` 或采集时刻推断。两端必须同时为带时区 `datetime` 且 `start <= end`，否则返回 `error/MALFORMED_RESULT`，不登记证据、不向模型暴露内容。相等端点表示单时刻，非 UTC 偏移保留并按绝对时间比较。
- 区间进入 `EvidenceRecord` 与模型可见 view，未知序列化为 `null`；视图哈希投影版本从 v2 升为 v3。区间不表示连续无缺口覆盖。
- `ToolRegistration.may_contain_secrets` 为必填严格布尔声明，覆盖原始 payload 是否可能含凭据、token 或其它秘密。缺失构造器拒绝；非 bool → `INVALID_SECRET_DECLARATION`；True → `SECRET_BEARING_SOURCE_FORBIDDEN`；只有 False 可注册。字段进入 `ToolRegistry.revision`。这是注册配置声明，不是扫描或脱敏保证，不能由模型输入决定。

## 测试与审查

任务先按旧实现运行新增测试，得到 5 failures（字段尚不存在）；实现后新增定向测试先 6 passed，补齐 paired naive/non-datetime、unknown、single-instant、offset、缺失声明后定向 `116 passed`。最终 `make check` exit 0，原样结论：

```text
All checks passed!
422 files already formatted
Success: no issues found in 18 source files
================= 1246 passed, 79 skipped, 2 xfailed in 27.91s =================
```

独立审查由全新上下文只读 Agent `/root/srcrange_review` 完成，独立定向 `116 passed`，无阻塞发现。审查边界仅覆盖本补丁和合同测试，不代表真实 adapter、PG 接线或产品验收。

## #29 接线建议（不改 #29）

在 `origin/feature/m1-01-investigation-loop:opspilot/investigation/reports.py` 的 `eligible_time_policies` 消费处，应从交付 view 读取并解析两个 source 区间字段；缺失、单端、naive/非法/倒置都 fail-closed，不把 requested `window` 或 `data_as_of` 当覆盖区间。历史策略应要求 `policy.window.start <= source_start_at <= source_end_at <= policy.window.end`；current 策略应要求完整区间位于 query/授权窗口内，并以较早的 `source_start_at` 相对可信交付参考时刻计算年龄，同时拒绝 `source_end_at` 晚于参考时刻。若继续沿现有 `eligible_time_policies` 返回 `frozenset`，建议新增关键字参数 `source_start_at`/`source_end_at` 与 `reference_at`，并在缺失/无效时跳过该 policy；调用方从 view 传入字段，保持 `freshness_seconds` 仅作兼容/显示，不作为完整区间资格替代。补对应红绿测试：区间超出 policy、区间超出授权 query、仅最新点新鲜但 start 过旧、未来 end、缺区间。不要修改 #29 现有代码或 passes。

## 边界与状态

未修改 `feature_list.json`、验收步骤、冻结哈希/上限、SPEC、PRODUCT-CONSTRAINTS、ROADMAP 或 #29；未启动 PG（本轮只改纯 dataclass/校验/视图传播，PG 路径未触及），未发起模型/网络/付费调用，未新增依赖，未合并 PR。PR #20 的 CI 需继续等待当前 HEAD；用户审核后合并。若 CI 或 review 出现新问题，应在 PR/任务记录逐条处置。
