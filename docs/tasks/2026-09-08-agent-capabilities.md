# Coding agent 文档接入与扩展能力规划

- 状态：已完成（本地接入与规划；尚未推送/合并）
- 更新日期：2026-09-08
- 依据：用户明确授权本项目 LangChain MCP，并要求不限 MCP 的 CLI+skill/执行验证等完整能力规划；[SPEC](../../SPEC.md)、[资源规划](../plans/delivery-and-resources-2026-09-08.md)。
- 工作区：接续 `chore/preflight-context`，`/Users/shenghuikevin/dev/AI/production-ops-agent-preflight`，起点 7e8a40e；该批尚未合并。主工作区仅安装用户授权的生成配置，不编辑业务源码或提交 main。

## 范围与完成条件

安装官方指南与 API 两个公共文档 MCP，提供可重复项目安装入口；保留既有宿主设置，不读业务秘密或使用其凭据，不增加用户级 MCP。资源规划按能力短板、复用/增加、时机、证据与成本覆盖完整开发周期，除 LangChain MCP 外本轮不安装候选工具。配置语法/幂等/保留设置测试、宿主读取与连接、公开资料实际查询及独立审查通过；明确活动会话加载与产品验收的界限。

## 当前证据

- 本机 Codex CLI 0.153.4、Claude Code 2.1.265；官方资料确认各自项目范围配置。主项目两者已有项目信任，task worktree 未额外设置永久信任。
- 安装入口输出只含文件路径；在 task worktree 与主项目均执行成功。Codex 主项目 `mcp get` 读取两个 HTTP server 和精确工具白名单；Claude 主项目 `mcp get` 对两个 server 均报告 Project config / Connected。
- 本轮从主项目 `.mcp.json` 读取端点，以 JSON-RPC `initialize`（2025-03-26）/`tools/call` 执行：`search_docs_by_lang_chain` 查询 `LangSmith trace without environment variables`，返回 [Trace without setting environment variables](https://docs.langchain.com/langsmith/trace-without-env-vars)；`search_api` 查询 Python `StateGraph`、limit=1，返回 [StateGraph API](https://reference.langchain.com/python/langgraph/graph/state/StateGraph)。两项均 initialized=true、success=true，无业务 key、无模型实验。公开返回摘录保存在 task worktree `tmp/docs-mcp-verification.json`，本段保留可重复的参数和结果来源。
- 在项目外 `/tmp` 执行 `codex mcp get langchain-docs --json` 返回退出码1 / No MCP server named，验证未新增全局服务器。主目录 `.git/info/exclude` 仅追加本批生成的四个明确配置路径（含 Codex 大小写变体），便于尚未合并时本地安装；版本管理的 .gitignore 保存相同规则，不隐藏其他文件。
- 安装测试初次 3 tests 通过。独立审查 agent `/root/mcp_install_review` 重现 P2：合法现有 inline table `mcp_servers = {}` 与追加子表冲突，可能写坏 TOML。已在首次写入前解析最终 TOML，拒绝不兼容表示而保留原文件，并新增回归；独立复验 4 tests 通过，P2 已关闭。未以首次测试掩盖缺陷。
- 最终 `make check` 通过（锁检查、Ruff lint/format、9 tests）；`git diff --check` 及 26 项本地链接/锚点检查通过。运行依赖、SPEC/PRD/feature_list.json 保持起点字节不变。
- 全新上下文 agent `/root/capability_plan` 独立审阅当前合同和能力缺口；建议以 M0 执行证据为优先，覆盖源码/LSP、CLI、trace/eval、API/UI、安全静态检查、交接/skill。该意见是规划审查，非工具运行验证。

## 下一步与交接

安装实现独立审查及 P2 复验完成，保存本地提交。生成配置位于项目本地、不进入 Git；仓库保存安装器与规则，后续 worktree 运行入口即可生成，不在用户级配置注册。主项目 .env 已由用户填写两个 key，当前仍无真实模型实验；安装过程不读取该文件。

本批未推送、PR 或合并，无服务常驻；当前对话尚未证明热加载，重开项目会话后使用新工具。配置可按开发说明停用；清理时保留用户后续定制。

## 后续真实工作

下一项为 M0-01：先实现显式配置加载、离线校验和有界实验 CLI，再确认 LangSmith 区域/项目与实际预算/期限，开展真实模型/trace 链路。其余 CLI、类型/秘密检查、API/UI/eval 能力的加入时机与验收见资源规划；本批没有安装这些候选工具。首次新会话实际使用 MCP 时核对宿主加载，不能将这次 CLI/公共 HTTP 探测称为正在进行的对话已热加载。
