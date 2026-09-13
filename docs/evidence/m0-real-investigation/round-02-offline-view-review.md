# Round 02 离线视图保真独立审查

日期：2026-09-10 UTC。审查者：fresh-context `offline_view_review`，未参与实现。

结论：**本次离线投影保真范围内无阻塞发现**。本轮 fault 与 normal03 的完整报告质量 FAIL 保持不变；没有重新调用模型，没有证明未来报告质量改善，也没有清除 M0 或产品实施门槛。

## 范围与版本

审查环境 worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment`，分支 `chore/m0-02-environment`，基线 `bc573cde6cbe9b925c9ae8b5859ad90b04e29711`。检查 AGENTS、SPEC、ROADMAP、C3 第 12 节及 M0 计划适用约束。候选为未提交的 holmes_baseline / legacy_projections 变更、trace_view、新测试与离线说明。

亲自校验七份当前源码与 `round-02-offline-view-source-manifest.json` 及对应快照逐字节相等；关键 SHA256：

- holmes_baseline：`e15d10cc9fc5ea523f8fd73af5e71e08596bc87481d6f78f1f0c8828ec783b63`
- trace_view：`0fdac927db8e633bdcae63d2dbacc988e3b3c5d66977ddddea22dbafb8b88f4b`
- legacy_projections：`0ed7d37aeca6057f2fd59ca2da692cab73b1247bd0e209555200ef74f32e27fc`

旧完整 wrapper 快照 `22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8` 重新计算哈希相符。未修改实现、历史 raw/view、账本或 Git index。

## 实际执行证据

使用主工作区 Python `/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python`，命令在环境 worktree 执行：

1. `-m pytest tests/test_m0_holmes_round02.py tests/test_m0_trace_view.py -q`：49 passed，1.59 秒。仅离线 fixture/临时文件/本地测试子进程，未启动真实环境服务。
2. `tests/fixtures/m0_environment/trace_fidelity_replay.py`：实际保留的 fault-01 和 normal-03 业务 raw 重放通过。四份 trace 的旧 v2 完整对象等于原 model view；两 Run 的全部 metric 原 v1 也等于原 model view；trace raw 执行前后 bytes 不变。
3. 独立内存探针：201 个汉字的 603-byte 字段裁到 600 UTF-8 bytes 并标截断；6 个诊断字段只显示 4 个且遗漏计数为 2；单 span 两类数量/字节截断标志均 false；无映射、服务冲突、多个候选身份均 unknown；metadata 自身超过 14000 bytes 时抛明确错误，不返回超限视图；未知 projection version 拒绝；源码及快照哈希检查通过。

实际 raw 重放结果与实现者结果一致：

| Evidence | raw traces/spans | visible spans | view UTF-8 bytes | raw/shown/omitted diagnostic fields |
| --- | --- | --- | --- | --- |
| fault-01-e8 checkout | 11/349 | 7 | 13755 | 143/14/129 |
| fault-01-e9 payment | 11/349 | 5 | 13381 | 143/20/123 |
| normal-03-e11 checkout | 5/248 | 14 | 13497 | 0/0/0 |
| normal-03-e12 payment | 5/248 | 13 | 12963 | 0/0/0 |

## 审查判断与限制

trace_view 将 backend 返回数、实际显示数、遗漏数与 caps 分开；字节裁剪每轮重建显示身份、parent_is_visible 和错误字段覆盖计数，所测样本没有把省略的 span 或错误文本称作已展示。仅保留列出的诊断键，coverage 明确此范围，不能解释成完整诊断字段清单。父引用标记只证明端点可见，不证明完整调用图。query.service 优先规则与 error/duration 排序通用，未发现症状类别路由或测试答案注入。

metric v2 保留 query/result，只增加缺失 series 不等于 0 的明确语义；这仍需后续模型遵守，不能当作模型误判已解决。新增详细错误字段导致 fault 覆盖降至 7/5 spans 是明确取舍；其余 trace 的具体错误仍 unknown。

14000 bytes 是该 JSON view 的 UTF-8 序列化上限；不代表整个供应商 HTTP 请求的字节长度或 token 数。整体 512 KiB 请求 guard 属于既有独立限制。已读实现者留存的 wire probe，未将其冒充本审查重新执行的 Holmes 集成证据。

本审查没有调用模型、trace、后端网络或环境操作，没有读取 .env、真实 private-protocol 或真实 reasoning。检查是离线合同与静态源审查；真实下一轮效果须另获授权后用冻结质量合同测量。
