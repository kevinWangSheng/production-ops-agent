# PR #27 收尾报告

任务：把 PR #27（`chore/instruction-contract-impl` → `main`）推进到 AGENTS.md 定义的
「PR 已就绪，待用户审核合并」状态，或明确报告为什么不能。

**结论：达到「PR 已就绪，待用户审核合并」。** CI 全绿、机器人四轮 review thread 全部
resolve、四轮独立审查（三份此前 + 本轮新派第四份）与机器人 `@codex` 均已逐条处置，
`mergeStateStatus=CLEAN`、`mergeable=MERGEABLE`。仍有三项需用户裁定，见下方，
本任务未自行选择版本，也未合并。

## 最终状态

- **HEAD**：`cf8f17d817e9baef2ff6c82b0fdb6679d628fa6f`
- **PR**：https://github.com/kevinWangSheng/production-ops-agent/pull/27（OPEN）
- **CI**：`checks` = SUCCESS，`m0-postgres` = SUCCESS
- **mergeStateStatus** = `CLEAN`，**mergeable** = `MERGEABLE`
- **review thread**：4 条，全部已回复并 resolve
- **本 PR 未合并**，等待用户审核

## 本轮提交列表（接手点 `090a63c` 之后，均已推送）

| commit | 说明 |
|---|---|
| `0d1668e` | 上一执行者留下的未推送提交：F10/F4/F14/`authorized_services` 类型护栏四条发现的修复 + `prompt_face_sha256()` 实现（接手时已存在，本轮验证后原样推送） |
| `cea8676` | docs：把「冻结证据覆盖面」的 docstring 断言收窄到只覆盖 `replay-candidate`（F2/R1 处置） |
| `e1a3336` | docs：记录三份独立审查的最终结论与仍需用户裁定的 U-a/U-b（接手时的未提交改动，验证后原样提交） |
| `dd20dcb` | fix：`render()` 先把 `authorized_services` 物化成 tuple 再校验/使用——堵住一次性迭代器（生成器）被校验那遍耗尽、渲染那遍拿到空列表的静默丢失（本轮新派的第四份独立审查发现） |
| `282331e` | fix：`template_projection()` 改为覆盖全部 segment（不只模板段），修复挪动 L1b/L2 槽位（如 `report_contract`）会改变 `render()` 字节但不 bump revision 的漏洞（机器人 `@codex` 本轮发现，P2） |
| `1856f85` | fix：没有预算槽位的变体（`baseline-final-report`）现在拒绝非 1 的 `model_requests`，此前会被静默忽略（机器人 `@codex` 本轮发现，P2） |
| `cf8f17d` | docs：改写任务记录「PR 交付状态」段，消除与「独立审查与处置」段的自相矛盾（机器人 `@codex` 本轮发现，P2） |

三处 `fix` 提交各自新增回归测试，并用 `git stash`（先复现测试对改动前的代码转红，再恢复修复）逐一复验；
未使用 `git commit --amend`/`rebase`/`reset`（被本会话权限规则拦下一次后，遵照用户指示改为原样保留、另开新提交）。

## `make check` 结论行

```
1098 passed, 77 skipped, 2 xfailed in 27.87s
```

基线（起点 main `b483a12`）为 `1050 passed, 75 skipped`；净增 48 个通过用例。
`.venv/bin/python -m pytest tests/test_instruction_discipline.py -q` → `48 passed`。

PG 持久化集成（`M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_durable_state_postgres.py`）
本次收尾**未重跑**——本机无 PostgreSQL/Docker（`pg_ctl`/`postgres`/`docker` 均不可用）。
最后一次实际验证结果沿用任务记录中更早一次有 PG 环境的提交：`23 passed`。这是本轮验证的
一处局限，如实记录，不虚构本次跑过。

## CI 结果

`gh pr checks 27`（HEAD `cf8f17d`）：

```
checks       pass   58s
m0-postgres  pass   31s
```

接手时记录的阻塞「`checks` 因他人分支 `feature/m1-01-intake-auth` 的 secret-scan 全 ref 扫描
命中而红」在本轮收尾重新拉取时已不再出现，未继续深挖是对方分支自行修复还是该次扫描本身瞬时；
因为已不影响本 PR，未做进一步处置。

## 独立审查：结论与处置表

### 第四份独立审查（本轮新派，全新上下文 sonnet subagent，只读）

按 AGENTS.md 要求以全新上下文、不继承讨论历史的 Agent 执行，只给它 PR 目标、C3 第 5/8 节、
diff 范围与既有发现清单，要求对「字节不变 / 冻结哈希覆盖面 / fail-closed」逐条给
file:line 证据。三点结论均为 **CONFIRMED**，且每条都直接复现（跑测试、构造反例、
读证据 JSON 原文），不是转述任务记录。另发现一条新问题：

| 发现 | 判定 | 处置 |
|---|---|---|
| `render()` 里 `authorized_services` 被迭代两次（校验一遍、拼接一遍），一次性迭代器（生成器/`map()`）在第一遍后被耗尽，第二遍拿到空的——校验通过但送进模型的授权列表悄悄变空 | **成立** | `dd20dcb`：先 `tuple(authorized_services)` 物化再校验/使用。新增回归测试，`git stash` 复验：改动前失败、改动后通过 |

U-a、U-b（见下方「需用户裁定」）经独立复现确认仍是代码里真实存在、未被悄悄了结的状态，未被此轮或之前任何一轮单方面解决。

### 机器人 `@codex` 本轮四条 review thread

`gh pr comment 27 --body "@codex review"` 手动触发（此前 PR 上唯一一条 codex 记录停在
过时提交 `59cafba` 且 Code Review/Security Review 均 `failed`）。本轮返回 4 条 inline
review thread，逐条复现后处置，全部已回复并 resolve：

| # | 位置 | 发现 | 判定 | 处置 |
|---|---|---|---|---|
| C1 | `discipline.py:264` | `template_projection()` 只筛 `LAYER_TEMPLATE` 段，挪动 L1b/L2 槽位会改变 `render()` 字节但不 bump `discipline_revision`，在途 Run 绕过 `blocked(INCOMPATIBLE_STATE)` | **成立** | `282331e`：投影覆盖全部 segment。`git stash` 复验 |
| C2 | `discipline.py:201` | `baseline-final-report` 无预算槽位，非 1 的 `model_requests` 被静默忽略（渲染字节、face hash 均与 1 相同） | **成立** | `1856f85`：无预算槽位的变体拒绝非 1 预算。`git stash` 复验 |
| C3 | `discipline.py:272` | `_short()` 截断 12 位十六进制是否符合 C3「比对仍用完整值」 | **不采纳，有依据** | 与已记录 U-a 是同一问题，两种读法都成立，回复引用 U-a 原文，交用户裁定，未改代码 |
| C4 | 任务记录旧第 504 行 | 「独立审查未完成」与同文件「独立审查与处置（三份」段自相矛盾，顶部状态行同样过期 | **成立** | `cf8f17d`：改写为前后对照段落，顶部状态行同步更正 |

三份此前的独立审查（F1–F19、R1–R5 等既有发现）逐条判定见任务记录「独立审查与处置（三份」表格，
未在本报告重复；全部已处置（成立并修复 / 部分成立 / 不成立且有复现证据 / 不自行裁定转交用户）。

## 仍未完成项

- **`ToolRegistration.description`（C3 第 8 节五字段结构体）本轮不做**：依赖
  `opspilot/tools/registry.py`（PR #20，`feature/m1-01-tool-executor`，仍 OPEN、未进 main）。
  本 PR 未动该分支，未在 main 上另建竞争 registry。
- C3 第 7 节七项确定性检查：第 1、4、5、7 项部分完成，第 2、3 项未做（均依赖上述注册表）。
- `ModelProfile.prompt_revision` 接线仍未完成——本轮只提供可确定性重算的来源，接线属
  M1-01「Flash 调查 loop」子任务。
- `holmes_baseline.py` 端到端 `prompt_sha256`（`023dae70…`）无法在 CI 复算（依赖 gitignore 的
  固定 HolmesGPT checkout）；本轮冻结的是该脚本 L1/L2 拼装字节的漂移断言，不是端到端哈希。
- PG 持久化集成本次收尾未重跑（见上方「`make check` 结论行」）。

## 需用户裁定的合同冲突（精炼版，供 2 分钟内决定）

**1｜`prompt_revision` 的 12 位截断短码，是否符合 C3「比对仍用完整值」？**
现状：`versions` 栅栏比对的是 sha256 的前 12 位十六进制（48 bit），没有任何 API 暴露未截断的
完整摘要。
- 选项 A（维持现状，推荐）：「完整值」= 完整的 revision **字符串**（前缀+短码），不是未截断哈希。零改动。
- 选项 B：「完整值」= 未截断的 64 位十六进制摘要。需要改 `discipline_revision`/`prompt_revision`
  对外暴露完整摘要，连带改已提交的 PG 测试夹具；不影响已冻结的 `prompt_sha256`。
- 后果：A 保持现状；B 是一次小改动 + 复验。

**2｜`template_projection()` 该只含模型可见字节，还是也含结构元数据（`key`/`tool_specific`）？**
现状：改名一个内部字段（模型可见字节完全不变）会让 `discipline_revision` 短码跟着变，
触发一次无意义的 `blocked(INCOMPATIBLE_STATE)`。（注意：这与本轮已修的「槽位顺序」是两回事——
顺序改动会真的改变 `render()` 字节，已经按机器人审查修好；这里问的是纯改名字该不该也 bump。）
- 选项 A（推荐）：投影收窄为只含模型可见字节。需放宽/删除一条测试——它援引的 C3 第 298 行
  实际写在工具注册表一段（L3a），可能本就适用错了层。
- 选项 B：维持现状，接受「改内部标注也 bump」是可接受代价。
- 后果：A 改一条测试断言方向；B 零改动但保留无意义 bump 的路径。都不影响已冻结的 `prompt_sha256`。

**3｜C3 第 7 节第 6 项（来源索引静态检查）与已批准的归位决定冲突**（沿用此前版本，未变）。
第 6 项要求来源索引做成产品模块内的结构化数据并加静态检查；但已批准的归位决定是该索引**不并入**
（取材自 M0 实验脚手架，无产品参考价值）。
- 选项 A：承接第 6 项，推翻「不并入」裁定，新增结构化来源索引模块。
- 选项 B：确认第 6 项随已删除的独立提案文件一并作废。
- 后果：A 是新增工作；B 是确认现状（本 PR 已经是 B 的行为，只是未经用户明确拍板）。

## 交接注意事项

- **保留了三个名为 `verify-*-WIP-*` 的 stash**（`verify-oneshot-iterator-fix-WIP-46942`、
  `verify-p2-projection-fix-WIP-33653`、`verify-final-report-budget-guard-WIP-92165`），
  内容均已体现在对应提交里，是复验过程中的临时备份，本身无害。`git stash drop` 被本会话权限
  规则拦下，未再尝试；可由用户手动 `git stash drop` 清理。
- 一次 `git commit --amend`（修正一条被 shell 反引号插值弄乱格式的提交信息）被同一权限规则拦下，
  已遵照用户指示放弃 amend，保留该提交原样（`282331e`，信息里两处反引号包裹的词因 shell 插值
  丢失，语义不受影响，未另开修正提交）。
