# 以开源对标和真实缺口驱动的项目路线

> 状态：历史讨论或研究证据，非当前规格。2026-09-06 已确认的产品边界以仓库根目录 SPEC.md 和 docs/adr/0001-readonly-investigation-boundary.md 为准；本文旧的候选、建议及待决表述不覆盖该共识。

日期：2026-09-06。阶段：项目前决策研究。没有部署、复现完整 Agent 或实现补丁。

## 决策建议

将开源对标明确为项目方法：固定上游 -> 运行原版形成基线 -> 复现问题 -> 确定改进 -> 保持兼容地实现/采用修复 -> 相同环境验证 -> 持续运行 -> 记录贡献。主对标及优先复用底座推荐 HolmesGPT。采用一个主底座，其它项目只用于专项能力参考或比较，不拼装多个完整 Agent 平台。

此前已考虑开源复用，但未建立“具体项目—已解决能力—真实缺口—我们的改动—验证”的逐项关系。当前补上这一决策输入。它不批准实现，也不把公开 issue 直接当作已复现的任务。

## HolmesGPT：主对标与优先代码底座

官方提供调查内核、多数据源工具、CLI/API/SDK、Helm 和 Operator 定时/变更触发；Operator 文档仍标 Alpha。复用其现有能力，不能把这些重新包装成原创。[仓库](https://github.com/HolmesGPT/holmesgpt)、[Operator](https://holmesgpt.dev/latest/operator/)

公开 GitHub API 当前返回最新正式 release `0.40.0`（2026-08-26）。本轮静态核查 `master` 固定 SHA `5e983c17f30e93099c7d775167266d4cd1d586c4`（提交时间 2026-09-01）。该 SHA 的源码发现与运行已发布镜像是不同证据；正式基线须固定实际代码、镜像 digest、配置和模型。[release](https://github.com/HolmesGPT/holmesgpt/releases/tag/0.40.0)、[commit](https://github.com/HolmesGPT/holmesgpt/commit/5e983c17f30e93099c7d775167266d4cd1d586c4)

### 候选一：工具调用的生命周期与超时

[Issue #2365](https://github.com/HolmesGPT/holmesgpt/issues/2365) 报告脚本工具在 Kubernetes API 连接异常时卡住并积累子进程。当前固定源码 `Tool.__execute_subprocess` 的 `subprocess.run` 调用仍未传 timeout，见 [tools.py L656–681](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tools.py#L656-L681)。这是静态确认；报告中的CPU/运行时间为用户自述，不当成本地测量。

已有 [PR #2369](https://github.com/HolmesGPT/holmesgpt/pull/2369) 提出进程组清理和超时实现，本轮 API 核查仍 open、未合并。应优先复现与验证已有补丁，避免重复开发。完整项目验证应覆盖：超时结束、后代进程清理、部分证据保留、明确工具失败、调查继续或交接、反复故障下资源不积累。不能仅以线程返回为成功。

### 候选二：模型参数验证与错误分类

[Issue #2376](https://github.com/HolmesGPT/holmesgpt/issues/2376) 报告 `timeout: null` 导致类型错误，却向操作者显示连接失败。API 当前仍 open。固定源码仍使用 `params.get("timeout", default_timeout)` 后与整数比较，instant/range 两条路径分别在 [prometheus.py L1611–1621](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/plugins/toolsets/prometheus/prometheus.py#L1611-L1621) 与 L1865–1875。

候选改进不限于一行空值处理：验证模型输入的空值/类型/范围，区分参数错误、连接失败、超时、无数据，使调查能正确处理缺失证据而不是误判基础设施故障。先确认所选版本真实调用链是否可到达，再开展改进。未复现整个工具/模型流程。

### 候选三：证据链接贯穿工具与报告

[Issue #2289](https://github.com/HolmesGPT/holmesgpt/issues/2289) 报告真实 `StructuredToolResult.url` 可被API客户端获得，但未传给模型，最终报告可能生成错误链接。API 当前仍 open。固定源码 [models.py L83–107](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/models.py#L83-L107) 的 formatter 拼接 metadata/data/params，未显式拼接 url。

候选改进是维护工具结果到证据引用到最终报告的映射；界面直接从结构化证据记录渲染链接，验证引用存在、时间/查询一致。模型生成错误链接的实际概率仍需运行验证，不能从静态省略推导发生率。

### 候选四：持续运行入口的完成语义

[Issue #2329](https://github.com/HolmesGPT/holmesgpt/issues/2329) 报告 scheduled checks 有时在“正在调查”的文字后结束，并出现 pass/fail 结果。API 当前仍 open；未定位当前源码根因、未运行复现。报告用例包含写动作，不直接纳入只读产品；可用只读等价流程研究“接受任务/调查中/调查完成/无法完成”是否被混淆。

该项作为待复现线索，不宣称当前版本必然有此缺陷。

## 改进范围与完整性

公开 bug 是入口，项目仍要完整运行：接入、事件/incident 管理、调查、人机交接、权限、部署升级、恢复、观测、质量反馈。逐项判断上游已经覆盖、配置即可获得、需修复、需扩展或超出本产品边界。不能把上游已解决的能力写成自己的创新，也不能将 issue 的 open 状态单独作为漏洞仍存在的证明。

目标可聚焦三项系统能力：工具失败可控且正确诊断；结论有完整证据引用；后台调查有明确状态并可恢复。每项必须通过真实运行的上游基线和修订版本对比；具体增量在复现后定。

## 下一决策建议

采用“基于 HolmesGPT 的完整二次开发产品”作为优先路线：保留上游调查/工具能力，维护小而清楚的改动，按需补充 incident 产品层，保持上游可更新。避免一开始承诺独立重写全部 runtime 或同时引入多套框架。

下一步决策对象是最终产品职责与支持边界，并将每项能力映射成复用/修复/扩展/不纳入。推荐只读值守调查、证据化结论、人工交接、恢复复查、可靠运营；写执行另设边界。具体工作负载与服务器方案在该映射之后处理。本轮不需要用户替代工程判断选择框架。
