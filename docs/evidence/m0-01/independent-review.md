# M0-01 离线准备独立审查

日期：2026-09-08（本地时区）。审查者：全新上下文独立 Agent `m0_independent_review`，未参与实现，只编辑本审查记录。工作区为 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`，分支 `chore/m0-01-offline`，起点 `0e51dd6979b33836125d1c6cc3cd18f15edd6c14`。

结论：离线准备审查通过；发现的 1 项 P2 已修复并独立复验关闭。没有未处置的阻断发现。本结论不表示真实协议、trace 服务、累计预算、持久恢复、完整 M0 或产品验收通过。

## 依据与范围

完整阅读 AGENTS.md、SPEC.md，并核对 ROADMAP、M0-01 任务、M0 计划 §1/2/6/7、C3 §5/11/12/13 及 feature_list.json 的相关验收。检查 `scripts/m0/`、合成 fixture、测试、pyproject.toml、uv.lock；后续复核开发指南、资源规划、能力任务和 M0/ROADMAP 状态更新。

独立检查实际代码、已安装 SDK 序列化路径和合成运行结果，不以实现者自测结论代替审查。未读取真实 `.env` 内容，未调用付费模型或远程 trace，未启动容器/数据库、安装工具、push 或 merge。

## 独立执行证据

- 首轮 `.venv/bin/python -m pytest tests/test_m0.py -q`：退出 0，23 项通过。
- 首轮 `make check`：退出 0，32 项通过；锁文件离线检查、Ruff 和格式检查通过。
- 用合成环境污染运行 `.venv/bin/python -m scripts.m0 offline`：设置 LANGSMITH_API_KEY/ENDPOINT/TRACING、OPENAI_LOG/ORG_ID、HTTP_PROXY、OTEL_EXPORTER_OTLP_ENDPOINT；退出 0，stdout/stderr 无合成秘密哨兵，stderr 为空。未使用真实凭据。
- 独立通过 CaptureSession 捕获完整 LangSmith 请求体：仅有 ID、时间、固定名称/项目/类型、固定 fixture 输入与白名单 outputs；虚假凭据和 provider 私有字段未进入请求体。此处仅为锁定 SDK 的内存序列化证据。
- 修复后定向测试：退出 0，31 项通过。
- 修复后 `make check`：退出 0，40 项通过（0.81 秒）；Ruff、87 个文件格式检查、35 包锁检查通过。pytest 插件列表仅 anyio，LangSmith 自动插件已禁用。
- 最后读取 [执行者原始证据](offline-verification.json)，用 hashlib 重算其 8 个源文件/fixture/锁文件哈希，全部匹配当前工件。该文件中的资源诊断与 SDK 签名是执行者记录，经阅读复核；独立测试结果以本节为准。

## P2：工具消息结构未随 ID 配对校验（已关闭）

初始位置：`scripts/m0/protocol.py:61` 的 `continuation`，原 64–77 行只核对调用 ID 集合和私有协议字段。保持 ID 匹配时，以下三种合成变体均被接受：工具结果 `role="system"`、assistant `role="user"`、工具结果 `content` 为字典。

这使离线声称验证的“完整工具消息组”边界不足；当前 CLI 只消费固定 fixture 且 live 永久拒绝，因此不是已发生的远程或生产越权。建议在复用该机制前拒绝非法角色、内容类型与消息结构。

实现者补充 assistant/tool 角色、content 类型、call ID/function 结构和结果精确字段集合校验，增加 8 个负例。独立重跑原三种变体，结果均为 `TOOL_PAIRING_INVALID`；上述定向和完整检查通过，发现关闭。

复现方法：读取版本管理的 fixture，调用 `tool_result` 生成结果，分别深拷贝并修改上述字段，再调用 `continuation(assistant, [result], provider="deepseek", run_id=RUN_ID)`。不需要私有数据或网络。

## 边界与交接

当前 live 在配置读取和 SDK 创建前无条件拒绝；offline 采用内存模型 transport、trace 捕获 session、环境清理和 Python socket 拦截。这里没有 OS 级网络隔离或生产授权证明，也没有持久累计预算、流式故障、数据库恢复、真实服务回读或容量测量结论。

新增任务合同明确上述缺项，六个未来真实用例与重复规则属于预先计划，不是已执行结果。产品 gate 和 passes 未被实验声明替代。真实 `.env` 的布尔检查由执行者随后执行，审查者未独立读取该文件，其存在性结果不视为凭据有效性证明。

下一阶段须补齐区域/项目与适用 workspace、实际调用授权及期限，并实现和独立验证累计预算、真实链路与 trace 回读。当前任务 worktree 和必要证据继续保留；本审查不授权新增外部动作。
