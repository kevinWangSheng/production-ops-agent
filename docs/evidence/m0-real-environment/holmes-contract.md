# HolmesGPT 真实开发基线合同 v2

执行前固定：2026-09-09。适用 M0 §5 / C3 §12 / SPEC upstream strategy，产品实施门槛保持关闭。

- 上游：HolmesGPT `5e983c17f30e93099c7d775167266d4cd1d586c4`，使用原 `DefaultLLM`、`ToolCallingLLM.call_stream`、`ToolExecutor`、`build_system_prompt`。源码不修改，Poetry 1.8.5 export 的主依赖使用 `uv pip --require-hashes` 安装；安装不是调查成功。
- 配置：官方 `https://api.deepseek.com/v1`、显式 `deepseek-v4-flash`、thinking enabled/high；LiteLLM OpenAI-compatible 路由；原上游共享 generic_ask prompt，加固定只读/证据/预算说明，不按故障类别路由。无 shell/kubectl/默认工具、外部知识和跨Run历史。
- 子额度：主账本 allocation `m0-holmes-20260909-baseline`，16 CNY / 16 次实际模型HTTP / 最多1 trace；本执行器关闭上游自动trace，未申请新增出口。每Run最多4HTTP；初始正常/故障各4与修复4，后追加4用于已有失败证据支持的复验；失败请求也占用。每请求预留1 CNY，不因低费用自行提高次数；最多128KiB请求，8192输出tokens，按峰值input3/output9 CNY/M作保守费用上界，实际账单仍需核账。
- 截止：2026-09-10T17:14:30Z；HTTP 180秒，单Run≤780秒；工具20查询/Run、20秒客户端IO超时（非逐工具绝对wall截止，未验证产品级取消/线程清理），代理另有限额/窗口/返回体积限制。128KiB请求按每byte=2 input tokens保守上界加8192输出，峰值0.86016CNY/请求，低于1CNY预留。累计请求账本位于 ignored `tmp/m0-environment/holmes-request-ledger.json`，主Agent汇总、不得重置。
- 凭据：可信客户端直接读取主仓库 `.env` 的模型key，只用于认证；不复制env文件、不输出或进入模型/工具/trace。清理继承环境中的trace、proxy及其他key；日志禁用。原循环内reasoning同Run续传；事件仅摘取最终业务content，永不序列化整体消息或AI_MESSAGE。
- 调查身份：固定 `m0-otel-20260909` 实例的只读API；显式四工具 services/metrics/logs/traces，source/URL不可由模型指定。模型端HTTP允许域名和路径，工具只用固定localhost API。此为开发进程的接口边界，尚不是OS/网络沙箱；同宿主开发者仍拥有全盘权限，不宣称盲测或完成全部权限验收。
- 前提：real_environment确认真实后端、正常流量/遥测及可诊断窗口后再调用模型。services配置列表不当作已采集证据。每案执行前保存不含注入答案的目标、症状/问题、窗口和可见证据清单；开发Agent可以评审答案，但不得送入运行调查者。
- 结果：保存配置/prompt hash、业务输入、每个工具完整query/observation/evidence_id、最终业务结论、HTTP次数/token/费用上界和失败。部署失败、遥测未就绪、协议失败、调查质量失败分别记录；无证据不能判定位正确。
- 通过判据：真实工具与模型链路完成且可追溯，正常案不虚构故障；故障案由独立工程证据核对定位/因果/反证支持。不以模型自述或框架完成表示效果通过；本轮开发小样本仅校准，不是匹配候选评测或泛化证明。

执行前检查已记录真实LiteLLM离线请求：顶层THINKING配置被LiteLLM拒绝（0 HTTP），调整为extra_body.thinking后enabled/high保留，工具续接reasoning_content仅布尔检查通过。独立审查发现并修复累计账本并发/原子性、HTTP读取体积、gzip重建和finish_reason误记完成；运行配置使用进程独占文件锁、每请求绝对180秒及全Run780秒计时。

2026-09-09 normal-02已有83148byte出站被65536上限拒绝证据，主Agent在原整轮20CNY/20HTTP总额度内追加Holmes4CNY/4HTTP，当前Holmes累计16CNY/16HTTP（保留前5次）；只把包络改128KiB，输出及每Run4HTTP不变。normal-03改用新固定窗口1788975849.645199..1788976149.645199，非同窗口配对。旧限制与失败保留在normal-01/02工件。

历史初始配置明确保留：v1为Holmes12 CNY/12实际HTTP、64KiB包络；normal-01/02按v1运行并失败。当前v2为16 CNY/16实际HTTP、128KiB，来自主累计合同在原整轮总额度内重新分配；主合同位于主任务工作区 `docs/evidence/m0-real-investigation/contract.md`，本子记录不取代主账本。

2026-09-09 补充有界收尾：主合同在执行前明确把未使用的最后1HTTP/1CNY分配给独立新Run `fault-handoff-01`。仅继承fault-02已持久的12个业务tool views与source/hash/实际窗口，不继承任何provider私有续传字段、不读取工程答案；上游loop `max_steps=1`，空ToolExecutor与工具HTTP硬拒绝确保0新查询，无trace。本地输入71493bytes、请求预估90493bytes小于128KiB；继续使用原累计ledger，15→最多16HTTP，整轮最多20。其结果只用于判断已收集业务证据能否支持报告，绝不改写原主动fault0/2。本次之后模型调用全部停止。
