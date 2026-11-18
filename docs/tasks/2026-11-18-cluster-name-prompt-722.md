# 调查提示词注入本地集群名（#722）

- 状态：进行中
- 更新日期：2026-11-18
- 依据：issue #722；[决定表 r10 H-U1](2026-11-12-tool-expansion-contract.md#r102026-11-18c5569与-666-预审裁决)；合同预审 `~/.agent/handoffs/opspilot-722-prereview-2026-11-18.md`（CN-1~CN-27、CN-K1，0 项用户裁决）；上游 HolmesGPT `a0009195` `holmes/plugins/prompts/generic_ask.jinja2:7-17`
- 工作区：`feature/722-cluster-name`，worktree `/Users/shenghuikevin/dev/AI/poa-722-cluster`（起点 `origin/main` `23f362f2`）

## 目标与范围

运维给扩展档位（`otel-demo-ext`）设了 `OPSPILOT_CLUSTER_NAME` 时，调查的 system 提示词在 Kubernetes 指导段之后、报告契约之前多一段多集群提醒：本地 Kubernetes 只看得到这个集群；其他数据源可能混有别的集群的数据，要按数据自带的集群、区域、环境字段核对，对不上就照实说明、记为缺口并标出真实集群。没设、设为空，或档位不是 `otel-demo-ext` 时，提示词字节、`prompt_revision`、Run 输入字节全部不变。

一个 PR（`feature/722-cluster-name`）：L1a 新段与新变体、Run 输入 v7、档位读配置与启动校验、C3 §5 L1b 行补一句（CN-K1）。不改 `deploy/docker-entrypoint.sh` 白名单与 Compose（CN-14），不改 `run_freeze`，不动 `feature_list.json` 与 `passes`（CN-25）。

## 公开接口（测试作者可用；改动先通知 lead）

- 环境变量 `OPSPILOT_CLUSTER_NAME`：只在 `OPSPILOT_TOOL_PROFILE=otel-demo-ext` 时读取，web 与 worker 都读，从传给 `select_profile` 的映射读（不读 `os.environ`）。未设或空字符串 = 未设。
- 名字规则：完整匹配 `[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}`（1–128 字符），前后空白不裁剪、直接拒绝。不匹配时 `opspilot.tools.profiles.select_profile(env)` 抛 `SystemExit("CLUSTER_NAME_INVALID")`（web 与 worker 同一入口，启动即失败）。`otel-demo`、`fixture` 不读该变量，设了任何值（包括非法值）都不报错、字节不变。
- 选档位与版本：`select_profile(env) -> ToolProfile`；`profile.versions()`（含 `prompt_revision`、`context_policy_revision`、`tool_schema_revision`）；`profile.face(clock) -> ToolFace`，`face.variant_id`、`face.cluster_name`（`str | None`）、`face.input_for(run_id=..., question=..., target_id=..., deadline=..., model_requests=...)`。
- 变体：设了时 `face.variant_id == "replay-candidate-k8s-cluster"`，`prompt_revision` 形如 `prompt-replay-candidate-k8s-cluster-<12hex>`，与名字取值无关；没设时仍是 `replay-candidate-k8s` / `prompt-replay-candidate-k8s-1137251eea4d`。`tool_schema_revision` 不随集群名变化。
- Run 输入：设了时 `scope_facts["cluster_name"]` 为名字字符串，`InvestigationInput.version == "opspilot-investigation-input-v7"`（常量 `opspilot.investigation.context.CLUSTER_NAME`、`INPUT_VERSION_CLUSTER_NAME`），版本优先级最高，可与其他事实并存。没设时不写该键，版本与字节同前。`InvestigationInput(...)` 构造与 `InvestigationInput.from_json(...)`：带 `cluster_name` 而变体没有集群槽、变体有集群槽而不带 `cluster_name`、名字不合规则，都是 `ContextError("INPUT_INVALID")`。
- 渲染 system 消息：`opspilot.investigation.context.initial_messages(input, evidence_context=None) -> (messages, system)`；名字取自输入里记录的值，不读环境变量。底层 `opspilot.instructions.render(variant_id, *, model_requests, report_contract, authorized_services=(), cluster_name=None)` 与 `prompt_face_sha256(...同参数)`：变体有集群槽而 `cluster_name` 为空、或变体无槽而传了值、或名字不合规则，抛 `ValueError`。`REPORT_CONTRACT` 在 `opspilot.investigation.reports`。
- 档位函数：`opspilot.tools.otel_demo_ext.otel_demo_ext_versions(*, elasticsearch=False, cluster_name=None)`、`otel_demo_ext_face(clock, *, elasticsearch=False, cluster_name=None)`；只看 `cluster_name` 是否为非空。
- web 与 worker 一边设一边没设：两边 `versions()` 的 `prompt_revision` 不同，认领被拦成 `INCOMPATIBLE_STATE`（既有 versions 比对，不新增代码路径）。两边设了不同的名字不拦（CN-11），模型看到 web 记录的名字。
- 未设时必须不变的钉值：base（`fixture`、`otel-demo`）`prompt-replay-candidate-860b743085d1`，system 7741 字符，sha256 `3908f85568327c52a402b46fa7b85d7749603c0f671fcded8aa0cd4f5cdf7cb2`；ext `prompt-replay-candidate-k8s-1137251eea4d`，system 12417 字符，sha256 `8e6cd839d72f3165f4bc0fb2303e79ba2f4841ced93e46e94af7a88bec8b15b8`。

## 合同（r10 H-U1 + 预审默认）

- H-U1（用户 2026-11-18）：集群名放进调查提示词，参考上游，未设时字节不变。
- 采纳预审默认 CN-1~CN-27 原样：只对 ext 生效（CN-2）；新变体而非条件段（CN-3）；位置在 K8s 段后、报告契约前（CN-4）；措辞用 CN-5 草稿，名字以反引号出现 3 次（CN-5、CN-7）；结论强度交 L2（CN-8）；校验与 `CLUSTER_NAME_INVALID`（CN-9）；两进程都读（CN-10）；值不进 revision（CN-11）；存 `scope_facts.cluster_name`、输入 v7（CN-12）；渲染两处 fail-closed（CN-13）；不进 entrypoint 白名单（CN-14）；续跑继承（CN-16）。
- CN-K1：C3 §5 L1b 行补「与运维配置的本地集群名（#722，H-U1）」，只记录已裁决内容。

## 执行进展与证据

- 进行中：实现与定向测试。

## 下一步与交接

- 有界真实 Run 的 LangSmith trace 与 `summary.json`：待 #671 短租冒烟（261 或 262 题，web 与 worker 都设 `OPSPILOT_CLUSTER_NAME`），合并前补进 PR（CN-24）。
