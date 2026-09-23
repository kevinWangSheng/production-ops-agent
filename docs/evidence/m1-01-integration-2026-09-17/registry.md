# 任务报告：C3 第 8 节 ToolRegistration.description 五字段结构体 + 第 7 节第 2、3 项确定性检查

- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-m1-tool-executor`，分支 `feature/m1-01-tool-executor`（PR #20）。
- 交接检查：`git status` 干净，`git log` 显示 head 已是任务书提到的「工具预算 ledger」提交（`f4f30fe`）——之前那个模型容量错误的 Codex agent **没有留下任何未提交或未跟踪改动**，无需沿用任何内容。

## 1. 字段设计与 C3 条文对照

C3 第 8 节「模型可见面」表（`docs/design/technical-proposal-2026-09-07.md:277-283`）：

| C3 字段 | 内容 | C3 要求的检查 | 本轮实现 |
|---|---|---|---|
| `returns` | 返回什么/数据源/投影形态 | 注册期非空 | `ToolDescription.returns`，`.strip()` 非空，否则 `ToolContractError("EMPTY_TOOL_DESCRIPTION_FIELD")` |
| `window_format` | 绝对时间窗的位置与格式 | 注册期校验占位符 | `ToolDescription.window_format`，须含 `{window}`，否则 `ToolContractError("MISSING_DESCRIPTION_PLACEHOLDER")` |
| `values_format` | 可用取值枚举的位置与格式 | 注册期校验占位符 | `ToolDescription.values_format`，须含 `{values}`，同上错误码 |
| `limits` | 结果上限与截断语义 | 注册期非空 | 同 `returns` |
| `cannot_prove` | 这个返回不能证明什么 | 注册期非空 | 同 `returns` |

字段声明顺序与 C3 表格顺序逐字一致，未来若建渲染器可直接按 `dataclasses.fields()` 顺序拼接，不需另立顺序规则。

**`{window}`/`{values}` 占位符字面写法是本轮自定，不是 C3 逐字要求**——C3 原文（`technical-proposal-2026-09-07.md:280-281`）只写「校验占位符」，未给出具体 token。已在模块内注释写明这是仿照仓库既有 `discipline.py` 的 `{steps}` 惯例（`str.format` 风格）自行选择，为将来的 L3a 模板/L3b 实例渲染器预留钩子，不代表 C3 已批准这个具体字符串。

`ToolRegistration` 新增必填字段 `description: ToolDescription`（`isinstance` 校验，非法 → `ToolContractError("INVALID_TOOL_DESCRIPTION")`）；`ParameterSpec` 新增 `description: str = ""`（默认空串）。C3 §8 的「结构体登记、注册期确定性校验」只列在 `ToolDescription` 五字段上，不要求逐参数也做结构完整性检查，因此 `ParameterSpec.description` 只做类型校验，不做非空校验——这一点已在 `ParameterSpec` 文档字符串写明依据。

## 2. 模型可见字节是否变化

**本分支此前没有任何真实注册的工具**：`grep -rln "ToolRegistration(" --include="*.py" .`（排除 `.venv/`）此前只命中 3 个测试文件，产品代码尚未把执行器接进任何组合层（`Workbench.run_once`/`Worker`），`ToolRegistration` 只在测试夹具 `tests/m1_tool_support.py::registration()` 里构造。因此：

- **不存在需要保持不变的已冻结/已记录模型可见字节**——`description` 是本轮新增的必填字段，没有「改变现有字节」的兼容性问题。
- `git grep -n "tool_schema_revision"` 确认它目前只在 `ModelProfile`（`opspilot/domain/`）与测试夹具里出现，**从未接线到任何持久化 `versions` 比对**，也没有任何写死的历史哈希字面量会被本轮改动打破。这一落差此前已记在任务记录第 7 节，本轮未解决（不在本次任务范围内），接线属后续 M1-01 组合层任务。

## 3. 测试

新增/修改文件：`tests/m1_tool_support.py`（新增 `description()` 构建器，`registration()` 与两个 `ParameterSpec` 补默认值）、`tests/test_m1_tool_registry.py`（`ToolDescription` 结构完整性用例）、`tests/test_m1_tool_registry_binding.py`（`CONTRACT_CHANGES` 新增两项：仅工具层 `description` 不同、仅参数层 `description` 不同）。

**PR #20 现有测试全部保留**，无一条被删除或弱化；`grep` 确认现有 `ToolRegistration(`/`ParameterSpec(` 构造点只有 3 个文件，均已处理（新增字段带默认值或经共享夹具承接，无需逐处修改测试断言本身）。

净增通过用例：`1233 - 1216 = 17`（详见下方「独立审查」，其中含审查后删除 1 条误导性用例）。

**变异验证**（每条新断言手工确认能转红，验证后 `diff` 对照保存的 `git diff` 补丁逐字节核对已还原）：

| # | 变异 | 结果 |
|---|---|---|
| M1 | 移除工具层 `description` 投影 | 对应 2 条测试（`CONTRACT_CHANGES` 的工具描述变化项）转红，其余 23 条不受影响 |
| M2（第一次） | 移除参数层 `description` 投影，测试条目本身省略了 `step_seconds` | **未转红**——省略 `step_seconds` 本身已改变参数集合，掩盖了 description 投影是否生效；这是变异验证自己揪出的测试设计缺陷，不是被动接受一次通过 |
| M2（修复后重跑） | 同一变异，测试条目改为显式保留 `step_seconds` 不变 | 对应 2 条测试正确转红，其余 23 条不受影响 |
| M3 | 移除 `ToolDescription` 全部结构校验 | 10 条断言转红 |
| M4 | 移除 `ToolRegistration` 的 `isinstance(description, ToolDescription)` 检查 | 1 条断言转红 |

## 4. `make check` 结论（真实输出，原样记录）

```
ruff check .          → All checks passed!
ruff format --check . → 422 files already formatted
mypy                  → Success: no issues found in 18 source files
pytest                → 1233 passed, 79 skipped, 2 xfailed in ~25-27s
```

`exit=0`。79 条 skip 全部是既有 PostgreSQL opt-in 用例；2 条 xfail 是既有架构欠债标记（与本轮无关）。

**未运行 PG 定向测试**：本轮改动只在 `opspilot/tools/registry.py`（纯 dataclass/校验/哈希投影），未触及 `opspilot/persistence.py` 或任何 PostgreSQL 集成代码；`grep -rln` 确认 `tests/integration/` 下没有任何用例引用 `ToolRegistration`/`ToolDescription`/`ParameterSpec`/`ToolRegistry`。按 AGENTS.md「运行与变更相称的检查」，判断本轮不涉及 PG 路径，未启动 `scripts.m0.postgres_lab`。

## 5. 与 PR #27 的依赖

未从 PR #27 分支 `opspilot/instructions/discipline.py` 复制任何符号或字节——该文件在本分支 `find opspilot -iname "*discipline*"` 为空，只存在于 `origin/chore/instruction-contract-impl`。本轮范围（C3 §7 第 2、3 项）本身不需要它的任何符号（`prompt_revision`/`Segment` 等只在做第 1/4/5/7 项补全或 PR #27 任务记录第 4 步「迁移 8 句 tool_specific 语义」时才用得上）。

`ToolDescription` 五个字段设计为纯字符串，为该迁移预留了承接位置，但**本轮不做迁移、不占位复制**——依据是 PR #27 任务记录明确写「待 PR #20 合并后承接」且该模块在本分支不存在，前置条件是 PR #27 先合并进 main。C3 §7 第 1、4、5、6、7 项按 PR #27 任务记录的既有结论（部分完成/暂停待用户裁定）原样保留，本轮未推进，也未在本任务范围内。

## 6. 独立审查

按 AGENTS.md「独立审查使用未参与该方案或实现的 Agent，并以全新上下文启动」，派发一个全新上下文、只读的 subagent，只给目标、约束、C3 §7/§8 原文、PR #27 任务记录背景与本轮 diff 补丁，不继承实现过程的结论。

**结论：可以按当前状态交回实现者，无阻塞发现。** 审查自己动手做了两次独立的变异实验（移除参数层/工具层投影后复跑、精确还原、`diff` 核对回补丁），确认 fingerprint 覆盖非空转、真实生效；确认边界纪律干净（`feature_list.json`/`SPEC.md`/`ROADMAP.md`/`PRODUCT-CONSTRAINTS.md`/依赖锁文件均无改动，未从 PR #27 复制代码）；确认 `ToolRegistry` 公共面未扩大；确认三个新错误码均为固定字符串、不拼接字段实际文本。

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 1 | should-fix | `test_registration_rejects_a_description_with_a_blank_required_field` 的注释声称验证 `ToolRegistration` 转发 `ToolDescription` 的错误码，但审查用 traceback 证实该场景在 `registration()` 被调用前、Python 参数求值阶段就已在 `ToolDescription.__post_init__` 内抛出，`ToolRegistration.__post_init__` 从未被进入——与另一条用例完全重复，且注释描述的代码路径（try/except 转发）根本不存在 | **采纳并修复**：删除该用例，在保留的 `test_registration_requires_a_structurally_complete_tool_description` 上补充注释说明「空必填字段」为何不会走到 `ToolRegistration` 一侧 |
| 2 | nit，不阻塞 | `window_format`/`values_format` 传非字符串（如 `None`）时报 `MISSING_DESCRIPTION_PLACEHOLDER` 而非类型错误码，语义上不完全对应 | **维持现状**：与 `returns`/`limits`/`cannot_prove` 传非字符串统一报 `EMPTY_TOOL_DESCRIPTION_FIELD` 是同一既有写法（单一代码覆盖一类违规），符合本文件既有错误码粒度惯例，不新增代码 |

处置后复验：`pytest tests/test_m1_tool_registry.py tests/test_m1_tool_registry_binding.py -q` → 98 passed；`make check` 全量复跑同第 4 节结论（`1233 passed`）。

## 7. 提交与 PR

- 提交（均已推送）：
  - `0e9c45c` — `feat: structure the tool description as a five-field model-visible face [#F7]`（代码 + 测试，含变异验证与独立审查处置说明）
  - `aff4586` — `docs: record the C3 §8 tool description and §7 checks 2/3 work in the task record`
- 推送前已 `git fetch origin feature/m1-01-tool-executor` 确认无上游漂移（远端仍为 `f4f30fe`），推送为快进，无冲突。
- 任务记录已更新：`docs/tasks/2026-09-14-m1-01-tool-executor.md` 新增「9. C3 第 8 节工具模型可见面（`ToolDescription`）与第 7 节第 2、3 项确定性检查」一节，并把第 7 节里「C3 第 8 节工具模型可见面尚未实现」的旧交接项标记为已完成并指向新节。
- PR #20 描述已更新（`gh pr edit --body-file`），新增「追加（2026-09-17）」小节，且把原「其它交接项」里的过期条目标记为已解决并指向新小节。
- CI：推送后 `checks`（pass）、`m0-postgres`（pass），均针对当前 HEAD `aff4586`。
- 机器人 code review：已手动触发 `@codex review`，覆盖当前 HEAD `aff4586a5d`，结果「Didn't find any major issues」，无新增 inline thread；此前三条 inline review thread 均已在更早轮次 resolve，本轮未新增未处理项。
- `mergeStateStatus`: `CLEAN`，`mergeable`: `MERGEABLE`，`state`: `OPEN`。

## 8. 未完成项（均已记入任务记录与 PR，非本轮遗漏）

- C3 §7 第 1、4、5、6、7 项：按 PR #27 任务记录既有状态原样保留，未在本任务范围内推进；第 6 项（来源索引静态检查）与「归位决定」的冲突仍待用户裁定，本轮未触碰。
- `PROJECTION_DISCIPLINE` 的 8 句 `tool_specific` 投影语义迁往 `ToolDescription` 字段：待 PR #27 合并进 main 后才可行，本轮只留好承接位置。
- `tool_schema_revision` 仍未接线到任何持久化 `versions` 比对（接线属后续 M1-01 组合层任务，非本轮落差）。
- F3/F7 的 `passes` 保持 `false`：本轮是注册合同的结构化与确定性检查，不构成产品验收证据。
- 本任务未合并 PR，等待用户审核；未触碰 main、未修改验收步骤/`feature_list.json`/冻结哈希、未新增依赖、未产生模型费用、未使用 force-push/rebase/amend/reset/stash。

## 终端最后一行

/private/tmp/claude-501/-Users-shenghuikevin-dev-AI-production-ops-agent/a7d3abb1-8221-4812-bafb-316b8b11e6aa/scratchpad/reports/registry.md
