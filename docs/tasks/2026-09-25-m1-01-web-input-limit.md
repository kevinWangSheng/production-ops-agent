# M1-01 剩余工作 3：网页追问上限改 8192 与 web 依赖默认安装

- 状态：进行中（待独立审查与 PR）
- 更新日期：2026-09-25
- 依据：ROADMAP「M1-01 已决（2026-09-24）」③④；「M1-01 剩余工作」第 3 项。功能 ID：M1-01。
- 工作区：分支 `feature/m1-01-web-input-limit`，worktree `../production-ops-agent-web-limit`（起点 `origin/main` `7ce8404`）。

## 目标与范围

③ 工作台网页追问/纠正文本此前校验上限是独立的 `opspilot/web/service.py` `_MAX_TEXT = 16_384`，比调查上下文实际允许的 `opspilot/investigation/context.py` `INPUT_CONTENT_FIELD_MAX_CHARS = 8192` 更宽；超过 8192 的部分会在 `project_input_content()` 里被静默截断并追加 `" …[truncated]"`，操作者对此毫无感知。用户决策（2026-09-24）：网页上限收紧到 8192，超限直接拒绝、不落任何记录，不再静默截断。

范围：`Workbench.control()` 的文本长度校验改用 `context.INPUT_CONTENT_FIELD_MAX_CHARS`（单一常量，不新增第二个魔数）；`incident.html` 的 `maxlength` 同步改为 8192；核对其余走同一白名单字段（`text`/`channel`/`question`）的入口是否受同一条截断规则影响。

④ 核实"`web` 依赖组默认安装"是否已经成立：`pyproject.toml` 已有 `[tool.uv] default-groups = ["dev", "m0", "web"]`，`make setup` 跑 `uv sync --locked`。若已满足，不改依赖或锁文件，只记证据。

不做：`DurableStore.control()`/`append_input()` 语义（另一 agent 在 `feature/m1-01-timeout-followup` 上改，不碰）；任何重构、依赖变更、其它 ROADMAP 项。

## 决定与理由（Agent 自决，可逆）

1. **只改 `_MAX_TEXT` 的取值来源，不改校验结构。** `Workbench.control()` 原本就是"先校验后落库"：长度校验（`len(text) > _MAX_TEXT` 触发 `ValueError` → `WorkbenchError("INVALID_INPUT")`）发生在 `self.ledger.put("control_intent", ...)` 之前（`service.py` 校验块在 232-241 行，落库在 260 行），本来就满足"拒绝而非截断、拒绝时不落任何记录"；本次只需把上限数值换成 `context.INPUT_CONTENT_FIELD_MAX_CHARS`，删除本地的第二个魔数。
2. **"可见提示"沿用现有的错误呈现方式，不新建 UI 机制。** 超限触发的 `WorkbenchError("INVALID_INPUT")` 经 `opspilot/web/app.py` 的 `_Refusal(400, "INVALID_INPUT")` 统一处理器渲染为 `{"code": "INVALID_INPUT"}` 的 400 响应——这与 `TEXT_REQUIRED`、`INVALID_ACTION`、`CONTROL_CONFLICT` 等现有校验失败完全一致的呈现方式，浏览器会显示这个 JSON 而不是静默跳转成功；不是内嵌到 `incident.html` 页面设计里的横幅提示。范围收紧到"拒绝且不落库"，呈现方式的进一步产品化（例如内嵌错误横幅）留给用户判断是否需要，未在本次实现。
3. **`intake.py` 的 `IntakeRequest.question`（`max_length=16_384`）与 `index.html` 的 `maxlength="16384"` 不改。** 核实结论：`question` 字段值经 pydantic 校验直接拒绝超长输入（同样不是静默截断），且这个原始问题只进入 `InvestigationInput.question`（`initial_messages()` 里作为固定前缀的一条 `user` 消息），从未经过 `project_input_content()`/`INPUT_CONTENT_FIELD_MAX_CHARS` 的那条 8192 截断路径——`INPUT_CONTENT_FIELDS` 里列 `"question"` 是给未来 `append_input()`（事件持续输入）路径预留的防御性白名单项，`opspilot/web` 与 `opspilot/investigation` 里目前没有任何调用点在用它，grep 确认（`append_input` 只在 `investigation/store.py` 定义与三处注释里出现，没有实际调用方）。因此这条路径不受本次决策影响，维持 16384 不变。
4. **④ 不改代码。** 见下方证据：`pyproject.toml` 第 22-24 行已是 `[tool.uv]\ndefault-groups = ["dev", "m0", "web"]`；本 worktree 的 `make setup` 一次 `uv sync --locked` 就装出了 `fastapi`/`jinja2`/`uvicorn`，无需额外 extra 或 group 参数。用户决策已经成立，不需要任何依赖或锁文件改动。

## 执行进展与证据

- 红/绿：新增 `tests/test_m1_web_workbench.py::test_control_text_over_the_shared_input_limit_is_refused_not_truncated`。实现前：`assert refused.status == 400` 处 `assert (200 == 400)` 失败（8193 字符的 `correct` 文本在旧的 16384 上限下被接受）。实现后：该用例与全文件 49 个用例全部通过。
- `make check`（本 worktree，`.venv` 由本任务 `make setup` 建立）：`doctor` OK；`uv lock --check --offline` 通过；`ruff check` / `ruff format --check` 通过；`mypy` "Success: no issues found in 42 source files"；`pytest` `1936 passed, 210 skipped, 2 xfailed`（对照上一条已合并任务记录的 1934 passed，多出的 2 个即本次新增用例覆盖的两条断言路径）。
- ④ 证据：`make setup` 输出（`UV_PROJECT_ENVIRONMENT=.venv uv sync --locked --python "3.12"`）新建 `.venv` 并 `Installed 46 packages`，其中包含 `fastapi==0.141.1`、`jinja2==3.1.6`、`uvicorn==0.53.0`——未传任何 `--group`/`--extra`，`default-groups` 已使其默认安装。`pyproject.toml` 未改动。
- 未跑真实模型 Run：本次改动只碰输入校验与模板，不涉及调查 loop、恢复路径或 `DurableStore.control()` 语义，AGENTS.md「验证与汇报」的真实 Run 门槛不适用；PR 描述中会如实说明。
- PG 集成：未跑，未触碰持久化层，`tests/integration` 未受影响（本地 `make check` 的 PG 用例按现有配置全部 skip，与改动前一致）。

## 下一步与交接

- 独立审查（全新上下文 agent，拿目标/约束/diff/上面的证据，不拿本记录的结论）→ 处置 P1/P2 → 提交 PR（用户门，功能 PR）。
- PR 就绪后：CI、一次 `@codex review` 分诊、CLEAN、0 未处理 thread；不由本任务合并，等用户合并。
- ROADMAP「M1-01 剩余工作」行只替换"3 网页 8192 与 web 默认安装"这一段为完成状态并加本记录链接，不动同行其它项（尤其第 2 项，由另一 PR 维护）。
