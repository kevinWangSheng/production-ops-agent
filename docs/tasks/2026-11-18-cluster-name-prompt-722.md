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

- 实现完成（本分支）：L1a 段 `CLUSTER_AWARENESS`（CN-5 草稿原句，6 句）与 L1b 槽位、变体 `replay-candidate-k8s-cluster`（`discipline.py`）；Run 输入 v7 与校验、渲染取记录值（`context.py`）；`ToolFace.cluster_name` 写入 `scope_facts`（`inputs.py`）；`InvestigationRequest.cluster_name` 让 `run`/`resume` 往返不丢（`loop.py`）；ext 档按设没设选变体（`otel_demo_ext.py`）；`select_profile` 读变量与 `CLUSTER_NAME_INVALID`（`profiles.py`）；C3 §5 L1b 行（CN-K1）。
- 钉值（同一脚本经 `select_profile → face.input_for → initial_messages` 计算，改前改后各跑一次）：未设与设为空时三个档位的变体、`prompt_revision`、`tool_schema_revision`、`context_policy_revision`、system 长度与 sha256、输入版本与改前逐字相同（见上节钉值）。设为 `prod-us-east-1` 时 ext：`prompt-replay-candidate-k8s-cluster-01c909abf29b`（48 字符，低于 trace outbox 的 64，CN-18），system 13594 字符，输入 v7，`tool_schema_revision` 仍为 `otel-demo-ext-c63f058afb18`；换一个名字 revision 相同、system sha256 不同；base 两档设了也不变。
- 一次性核对脚本（不入库，测试由独立作者写）：设了时 system = 未设时的字节在报告契约前插入 1153 字符，名字出现 3 次；web 映射与带 K8s/ES 配置的 worker 映射 `versions()` 相等；8 个非法名（前后空白、换行、反引号、花括号、129 字符、`-` 开头、`=`）都 `CLUSTER_NAME_INVALID`，`otel-demo` 与默认档设非法值不报错；EKS ARN 形状和 128 字符通过；v7 `as_json → from_json` 原样读回；v7 配无槽变体、无名字配有槽变体、非法名都 `INPUT_INVALID`；从记录的输入重建 system 与提交时逐字相同。
- 检查：`ruff check`、`ruff format --check`、`mypy`（7 个改动模块）通过；`.venv/bin/python -m pytest tests/test_tool_expansion_b1_contract.py tests/test_architecture.py tests/test_m1_report_contract_citations.py tests/test_tracing.py` 259 passed、2 xfailed（既有标记）；`tests/test_m1_otel_demo_contract.py tests/test_holmes_evals_questions_contract.py tests/test_686_report_json_contract.py` 41 passed。全量与 PG 测试未在本机跑（swap 13.5/14.3 GB），交 CI。
- 实现者自定（可逆）：`render` 的 `ValueError` 在 `initial_messages` 里统一转成 `ContextError("INPUT_INVALID")`（其余 `ValueError` 来源已被 `InvestigationInput` 构造校验挡住，实际只剩集群名一类）；`render` 对名字再按同一正则校验一次，防直接调用方绕过档位校验。

## 下一步与交接

- 有界真实 Run 的 LangSmith trace 与 `summary.json`：待 #671 短租冒烟（261 或 262 题，web 与 worker 都设 `OPSPILOT_CLUSTER_NAME`），合并前补进 PR（CN-24）。
