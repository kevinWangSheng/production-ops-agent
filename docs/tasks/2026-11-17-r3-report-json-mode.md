# r3 报告格式：每轮开 JSON 模式（#686）

- 状态：进行中
- 合同修订：r1（2026-11-17）
- 依据：issue #686；lite-full-1117 首答被拒 39/105，其中一大块是作答回合带前置文字或代码围栏；r3 发布的另一半是调查指导 PR #697（同一次租机验收）。

## 目标

调查每一轮请求都带 `response_format: json_object`（压缩请求除外），从源头去掉「前置文字 / 代码围栏」这类格式失败；返回空白按失败处理。顺带：hypothesis / recommendation 类 claim 缺 `evidence_ids` 默认空列表（U2），缺 `next_steps` 默认空列表（U3），引用出错时重试提示给出唯一最近的合法 id（D6）。

## 合同（r1）

决定表全文：[2026-11-17-r3-report-json-mode-contract.md](2026-11-17-r3-report-json-mode-contract.md)。用户裁决：

- U7（2026-11-17）：每一轮都开 JSON 模式，接受工具轮不再有可见进度说明。依据：真实模型实验 33 次调用，tools + json_object 同开 0 报错、工具轮照常调用工具、作答 10/10 纯 JSON（实验报告在 r3 交接目录，脚本与原始响应未入库）。
- 去壳（前置文字 / 围栏剥离）取消；原 U1、U4、U6、D1–D5、N1–N3 作废。
- U2=a、U3=a、U5=a（预审确认不再必需，不改该测试）。
- 可逆默认 R1–R10 按推荐：R1 压缩请求不开；R2 `json_mode=final` 改恒真，字段保留；R3 不加版本，`prompt_revision` 不变；R5 空白沿用 `EMPTY_REPORT` 现有路径；R6 工具轮空白不算失败、原样回放；R7 P1 工具轮解析与回复说明模板不动；R8 撞输出上限沿用 `OUTPUT_LENGTH`；R9 只改 DeepSeek 参考与工作台合同两处注；R10 估计器不补算。
- G1（F3 旧证据是否作废）由 lead 定为不需裁决：r3 租机会在 #697 分支（含本 PR）上重跑 F3，新证据就是本行为下产生的；main 上现存 F3 证据只是历史，F3 `passes` 本来为 false。

## 分工与 worktree

| 角色 | 执行者 | worktree / 分支 |
|---|---|---|
| 实现 | Claude Opus 5.5 子 Agent | `../poa-686-impl` / `feature/686-json-mode-every-round` |
| 验收与合同测试 | Codex（codex-role test），不看实现 | `../poa-686-tests` / `test/686-json-mode` |
| 独立审查 | Codex（codex-role review） | 只读 |

## 证据

- 实现（2026-11-17）：`loop.py` 调查轮 `json_mode=True`、压缩请求仍 False；`reports.py` U2/U3 默认空列表、D6 重试提示（唯一最近的已交付 id；`missing_target_refs` 列出出错的 ref 与允许值，各最多 50 条）。报告合同、`FINAL_REPORT_INSTRUCTION`、重试模板字节不变。
- 定向测试：按 U7 预期失败 2 项（`test_m1_investigation_loop.py` 钉工具轮 `json_mode is False` 的两项，由测试作者翻转）；D6 与 #456 两条既有断言冲突（`test_456_forced_final_fence_feedback.py` 的 `..._matches_what_the_citation_gate_says_unfenced`、`..._the_valid_one_is_not`），交 lead 裁决；其余定向文件全过。
- 有界真实 Run：[证据](../evidence/686-json-mode-live/README.md)。4 个模型请求 `json_mode` 全为 true；首个 Run 纯 JSON 报告、`INCOMPLETE_INVESTIGATION`；续接 Run 终答轮返回不带 `schema_version` 的 `{note,replies,report}`，`RESPONSE_INVALID`。0.077032 CNY。

## 待决

无。
