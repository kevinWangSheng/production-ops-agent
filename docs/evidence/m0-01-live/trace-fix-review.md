# Trace 回读兼容修复独立审查

2026-09-09；审查者为未参与实现、以全新上下文启动的独立 Agent。工作区 `production-ops-agent-m0-01`，基线 `a99d1f6`，审查其上未提交的 `scripts/m0/live.py` 与 `tests/test_m0_live.py`。

结论：本轮限定修复未发现阻塞问题。完成独立离线验证；没有重跑模型、上传 trace、发送外部请求或连接 PostgreSQL。没有读取 `.env` 或 approval 文件。结论不代表原 CLI 已成功、数据库 trace 状态已修复、M0 退出或产品验收通过。

## 依据与核验

已读取 AGENTS、完整 SPEC、ROADMAP、C3 §11/12、原 `execution.md`、出口/回读与 execute 控制流。旧判断要求回读 extra 为空；新判断只在入站接受平台 root 标注 `metadata.ls_run_depth` 为精确整数 0，且不接受其他 extra 或 metadata 键。出口 `trace_wire` 仍拒绝非空 extra；本轮没有扩大 DTO 或数据出口。

只在本地读取已有 `trace-readback-private.json` 与 `ledger-result.json`，未输出原始回读正文。独立逐字段检查确认捕获的 run UUID 与账本一致、outputs 与账本 outbox 的所有键值和 Python 类型一致；输入 fixture 和 root extra 形状符合新校验。进一步使用主执行者提供、来源为此前项目 GET/UI 核验的固定预期 project UUID 复验，得到 `TRACE_VERIFIED`。审查者没有自行重新请求平台，也没有独立重新核查该 UUID 的账户来源；此身份来源边界予以保留。

这证明当前捕获可被修复后的校验器正确识别。原执行没有保留当时响应正文，因此不能证明当时第二次回读的内容与当前捕获完全一致；root metadata 可确定复现旧校验的误判，属于强支持的原因解释。

## 主动反例与测试

- 独立运行 `.venv/bin/python -m pytest tests/test_m0_live.py -q`，最终 **28 passed**。覆盖完整 execute 内存 HTTP 路径，包括真实 metadata 形状、非法 metadata、额外私有键、false extra，以及 HTTP 200 JSON null。null 现在返回固定响应格式错误，不再当作 404 重读。
- 独立临时 Python 反例脚本在本地执行：20 个非法回读变体全部拒绝，包括 extra 的错误形状、bool/float/string depth、非零 depth、多余 metadata、身份不符、输入夹带字段、非对象响应和输出 bool/float 类型污染。
- 用 `no_network()` 与 SDK 内存捕获注入两种非空 metadata（包括入站允许的 root depth）：两者均被 `trace_wire` 拒绝，证明入站兼容没有放开出口。
- 异常分类只允许固定错误码，未把响应或异常正文写入 CLI；业务结果与 trace 结果仍分别报告。此次代码审查不证明所有网络故障或供应商未来响应形状。

## 固定受审文件

- `scripts/m0/live.py` SHA-256：`ecf55a070b139a8a49dbd81dc36709ff8d187c9e29a8869c43bf72f8aa00856c`
- `tests/test_m0_live.py` SHA-256：`2dd0be47220d844bef6db4a5720a507555f79c7bc24743c1113625b9d1fbf8f1`

没有新增产品功能、费用授权、验收步骤变更或合并授权。原有历史 unknown 记录应保留，并将本次离线诊断结论作为后续证据追加。
