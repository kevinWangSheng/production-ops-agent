# 合同预审：M3 judge 重校准（F1 第 5 步，#295）

- 预审者：全新上下文、只读，未参与 #300 实现。核对版本：main `db5360c3`，PR #300 head `a80bdb15`（`chore/p7-r3-judge`），合并基点 `8f1f49cd`。
- 前提（不重议）：用户 2026-10-22 裁决 r3（P7-D17–D21），2026-10-27 Q7 按推荐继续（`docs/tasks/2026-10-27-m2-exit-m3-opening-decision.md:53-57`）；#312 保持开放、不实施（#312 评论）。
- 文中「分支记录」指 `origin/chore/p7-r3-judge:docs/tasks/2026-10-18-eval-p7-calibration.md`；代码行号按探针 worktree（分支 + main 合并后）。
- 裁决（r1，用户 2026-10-28）：J1–J3 按推荐（决策稿第二轮 Q10）、J4 按推荐（Q11）、J7 改为「astra → gpt-6-sol → Fable 5.1，用 Fable 的批次披露两份盲标同属 Anthropic」（Q12）；J5、J6 属可逆默认，按推荐由 Agent 定；可逆默认表按推荐。#300 合并前补一次全新上下文独立审查，重点核对 `503e3a80` 放宽的断言是否符合 D19。见[决策稿第二轮](2026-10-27-m2-exit-m3-opening-decision.md#第二轮裁决2026-10-28)。

## 合并与探针

- **合并冲突：无。** 分支落后 main 55 个提交；main 一侧在评测目录只改了 `opspilot/evaluation/tool_provenance.py`（`git diff 8f1f49cd origin/main --stat -- opspilot/evaluation scripts/eval_p7.py …`），与 #300 改的 14 个文件不重叠。`git merge origin/main` 自动完成。
- **探针 1（只合并，不打补丁）**：`.venv/bin/python -m pytest tests/acceptance -q` → `2814 passed, 299 skipped in 534.99s`，**探针 0 失败**（跳过的都是需要 `M1_DURABLE_POSTGRES=1` 一类显式开关的用例）。
- **探针 2（合并 + 最小 PROMPT 草稿补丁）**：在 `judge.py` PROMPT「Judge each field by its main assertion.」之后插一句（模拟一轮调参改动）后再跑全量验收；结果见文末「探针 2」。
- PR 状态：`mergeStateStatus: BEHIND`；review thread 0 条；Codex 机器人 3 次「Something went wrong」（按 AGENTS.md「机器人不可用不阻塞」）。PR 正文写明「未完成项：独立审查」，仓库与任务记录都找不到审查记录（`a80bdb15 review fixes` 的提交信息说明有过审查，但审查者和发现没有留档，未确认）。

## 需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| J1 | **「46/48 等价」在 36 道调参题上怎么算？** r3 原文只写「一致率不低于 46/48 等价」（分支记录:50）。正式校准是 24 道 validation 各判 2 次，共 48 次比较；debug 只判 1 次（`calibration_flow.py:707-713`）。调参工具只输出 `verdict_match`/`key_hit` 两个计数（`eval_p7.py:1951-1971`），不算误通过、无效和一致率，所以达标线目前没有代码实现，实现者只能自己猜 | A：照正式的形状排练——12 道原 debug 各判 1 次，24 道原 validation 各判 2 次（每家每轮 60 次调用），直接复用 `_counts` 的口径：24 道 ×2 的一致数 ≥46，60 次观察全部零问题。最贴近正式，也能测到重复判分的不稳定；费用约为 B 的 1.7 倍。B：36 道各判 1 次，按比例要求 ≥35/36（36×46/48=34.5，向上取整）。便宜，但测不到重复判分的不稳定，而且「35/36」是新造的数字。C：36 道各判 2 次（72 次），要求 ≥69/72 | **A**。正式校准用什么口径和计数器，调参就用同一套，不另造数字；零误通过沿用 P7「负例两次都不得误通过」（`2026-10-12-eval-suite-contract.md:138`） | 达标判定、每轮费用、需要新增的调参计分器 | |
| J2 | **一轮达标看哪一家？** 三家共用一份 `judge.PROMPT`。选择规则：zai 合格、且一致数不低于 openai 才自动选中；只有 openai 合格时为 `pending_user_decision`（`calibration_flow.py:819-832`）；DeepSeek 只作对照、不能当选（`2026-10-14-eval-pr-b3-interface.md:20`）。r3 没说调参轮的达标线是哪一家要过 | A：只看 FlashX（主候选）达标；Luna 照跑并记录，不影响停止条件。B：FlashX 或 Luna 任一家达标即可；只有 Luna 达标时照 P7-D14 停下交你，再决定是否以 Luna 进正式校准。C：两家都要达标 | **B**。和正式校准的选择规则一致，不浪费一家已经达标的轮次；DeepSeek 达标不算 | 停止条件、能否进入新 validation 阶段 | |
| J3 | **「最多 3 轮」从哪一轮算起？** #300 已经把 PROMPT 改成 r3 文字（D17 约定段、D19 因果口径），合并后第一次真实 debug 本身就是在测一个改过的 PROMPT | A：#300 合并后第一次 36 题运行算第 1 轮，之后最多再改 PROMPT 两次。B：#300 那次算第 0 轮（只测基线），之后另有 3 轮改 PROMPT 的机会，最多跑 4 次 | **A**。r3 的四项本来就是修复内容，单列第 0 轮等于多给一轮；如果第 1 轮达标，之后也不用再调 | 走到 #312 之前的尝试次数、费用 | |
| J4 | **环境性失败会不会消耗一轮？** 调参的零无效包括断连、HTTP 错误和超时（正式校准里这些都是 `JUDGE_OBSERVATION_FAILED`，整家不合格，`calibration_flow.py:780-790`）。Q7 写明分诊只是归因视图、全部尝试都计入分母（决策记录:56）；Q10 预授权的「环境失败每场景重跑一次」针对的是 F1/F8/F9/F14 实施 PR 的真实运行，没有说是否适用于调参轮（决策记录:68） | A：一轮里只要有任何失败就判本轮不达标，并消耗一轮。最严，可能因为本机网络断流用掉 3 轮。B：本轮照样判不达标、全部计入，但如果失败只分诊为「评测/环境」且 PROMPT 没改，可以原样重跑一次，重跑不算新的一轮；原记录保留，不覆盖。C：环境失败从这一轮里剔除 | **B**。不改变计数口径，正式校准仍然零重试；只是不让网络问题吃掉有限的调参次数。C 与 Q7「全部计入」冲突 | 轮次预算、Q10 授权范围 | |
| J5 | **调参中能否改标签？** #312 选项 2 就是「复核三家重叠漏诊的标签口径」。formal-2 的主要失分是约 6 道题三家都漏列（summary/claims 对同一原因的重述），D17 只放宽 quote，对完全没列出那个位置的诊断不起作用（分支记录:44；#295 正文）。调参时如果确认是标签有问题，实现者没有依据决定怎么处理 | A：标签冻结，疑似标签问题在分诊里标为「评测问题」并列出证据，3 轮结束后和 #312 一起交你。B：允许你中途裁决改个别标签（等于提前执行 #312 选项 2，改了以后前面各轮不可比）| **A**。r3 没授权改标签；改标签属于 #312 范围 | 标签、#312 的时机 | |
| J6 | **新 validation 阶段的工作区组成。** freeze/formal 要求 LabelPack = debug 项（各判 1 次）+ validation 项（各判 2 次），而且 debug 项出现任何无效或漏诊，整家同样不合格（`calibration_flow.py:707-713, 760-799`）。旧工作区 `assembled/items.json` 只能写一次（`eval_p7.py:1210-1216` `_write_new`），所以新 validation 必须新建工作区。新包里放哪些 debug 项，没有任何地方写明 | A：新工作区整套新采——6 个场景各 2 个新的 live 来源族（12 次 live，约 2 小时 lab），独立作者写 12 debug + 24 validation，两位标注者标全部 36 项，你最多裁定约 36 项；与 Q7 估计的「最多约 36 项」（决策记录:57）以及 P7-D2/D5 的形状一致；formal 每家 60 次调用。B：只新采 24 道 validation，debug 位放入已调参的旧题（D21 允许把已暴露的族作 debug 复用）；lab 时间减半、需要裁定的项变少，但旧题的标签是在旧工作区裁定的，能否导入新工作区未确认（导入要求标注绑定新的盲标包，`eval_p7.py:1272-1351`），而且 debug 位就是调过参的题，在 formal 里等于送分 | **A**。工具链不用改，用户投入与 Q7 的估计一致；B 需要新写导入路径，旧题在 formal 里也会注水 | 实验室时长、你的裁定工作量、formal 费用、工具改动 | |
| J7 | **标注者不可用时能否替换？** 两个槽位固定为 slot 1 `claude-opus-5-5`、slot 2 `gpt-6-astra`（`2026-10-19-eval-p7-driver-interface.md:265`）；SPEC 的数据出口扩展只批准这两家接收评测输入（P7-D15）。近期 astra 多次出现容量错误（本机记录：astra→sol→Fable 兜底），r3 对此没有规定 | A：只用这两个固定身份，astra 不可用就等。B：预先批准同家族替补（OpenAI `gpt-6-sol`；数据出口仍是 OpenAI，但身份变了，需要改 SPEC/D8 的条文）。C：换成其他家族（新的数据出口，属于 Q10 停下项） | **A，并预先批准 B 作为等待超过 48 小时后的替补**，替补身份写进 annotation 的 evidence_note。B 没有新增供应商，只改身份条文 | 数据出口、P7-D8/D15、盲标独立性 | |

## 可逆默认（Agent 自定，一行理由）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| R1 | 一轮调参能改什么 | 只改 PROMPT / 也可改 adapter 参数（reasoning_effort、response_format、max tokens）/ 也可换模型 | **只有改 `judge.PROMPT` 算调参**（P7-D16 原文「调优只改 judge.PROMPT」，分支记录:34）。如果 zai strict json_schema 失效（D20 标为「调参轮继续验证」，`judge.py:772-781`），回退到 json_object 属于 D20 的可逆技术项，可以做，但要升级 adapter 版本，并且同样消耗一轮；换模型属于费用和身份变更，停下交用户 | 轮次定义 | |
| R2 | 每轮的代码怎么落地 | 每轮一个 PR 合并到 main / 在一个调参分支上每轮一个提交 | **在调参分支上每轮一个提交**，记录 commit、`judge_prompt_sha256` 和 `judge_config_hash`（config hash 含 prompt，`judge.py:242-256`）；达标后只合并最终 PROMPT 的一个小 PR，3 轮不达标则不合并、保留分支给 #312。`--tuning` 跳过 PROMPT 逐字绑定，可以在分支上运行（分支记录:52）；formal 前 PROMPT 必须已经进 main | PR 数量、可追溯性 | |
| R3 | 调参计分器 | 人工数 / 补一个只读计分命令 | **补一个只读的 `tuning-score`（或扩展 `_debug_comparison`）**，直接复用 `_counts` 的问题集合（`JUDGE_OBSERVATION_FAILED`、`KEY_DIAGNOSIS_MISSING`、`P6_RECHECK_FAILED`、误通过、一致数），按 J1 的形状输出；不碰 formal 路径 | 达标判定可复现 | |
| R4 | DeepSeek 是否参加调参轮 | 三家都跑 / 只跑 FlashX + Luna | **只跑 FlashX + Luna**，DeepSeek 只在新的 formal 中跑（`_PROVIDERS` 要求三家都到场，`calibration_flow.py:77, 838`）。理由：DeepSeek 不能当选，每 60 次调用约 15.6 CNY（共享账户上界），是最贵的一项 | 费用 | |
| R5 | 每轮费用与预算上限 | — | 估算（按 p7-debug-10 与 formal-2 的 token 和官方价，未实测）：J1-A 每轮 FlashX ≈ $1.4、Luna ≈ $0.9；3 轮 ≈ $7。新 formal（J6-A，每家 60 次）≈ FlashX $1.4 + Luna $0.9 + DeepSeek 15.6 CNY。C3 judge 分配 100 元（`technical-proposal-2026-09-07.md:439`）；目前已花的 judge 费用只有 DeepSeek 余额差可查（P7 记录中各轮合计约 27 CNY，共享账户），zai/openai 的早期轮次没有记录，**未确认**。合计超过 100 元的 80% 时停下找用户（Q10 费用停下项） | 费用 | |
| R6 | 三类分诊怎么记、记在哪 | — | 每轮每个失败一行：`round, provider, item_id, repetition, code, 类别（评测/环境 · judge 误判 · 真实失败）, 证据路径（.raw / 观察 json）`，放在私有工作区 `tmp/eval-p7/<ws>/triage/round-N.json`（被 git 忽略）；任务记录里只写各类计数。题面和标签不进仓库和 issue（ADR-0006，仓库公开）。被测对象是 judge 本身，所以「真实失败」在调参轮里没有对应项，标为不适用；疑似标签错误归「评测问题」（见 J5）。分诊不影响达标判定 | 归因视图 | |
| R7 | 供应商异常或版本变化 | — | 响应 model 与固定身份不一致时，现有代码判 `JUDGE_RESPONSE_IDENTITY_INVALID`（GLM 要求完全相同，`judge.py:117-125`），本轮按无效计，并停下报告。同名模型静默更新无法检测：每轮记录日期和 response_model；最后一轮调参与 formal 之间间隔超过 14 天的，formal 前先跑一次 12 道 debug 复核。FlashX 下线或改名属于换候选，停下交用户。单次超时 900 s、传输重试最多 1 次不变（`judge.py:203, 874`） | 外部调用 | |
| R8 | 盲标怎么保证盲 | — | 沿用驱动接口 Q8-A（`2026-10-19-eval-p7-driver-interface.md:458`）：盲标包复制到仓库外目录，Claude 以全新非交互进程运行、工作目录限于副本，Codex 只读沙箱；另外**收紧一条**：调参实现者（看过旧标签和 judge 输出）不能担任新的改写作者或标注者，新改写作者也要是全新上下文（P7-D7 的延伸）。标注说明使用 D19 原文（盲标包已复制整段 PROMPT，见 `tests/acceptance/EVAL_P7_R3_AMBIGUITIES.md` 第 6 条） | 盲标独立性 | |
| R9 | 合并 #300 前的就绪条件 | — | 合并后的 HEAD 需要 CI 绿灯，并由全新上下文 Agent 做独立审查（PR 正文仍写未完成）。审查要专门核对 `503e3a80`：实现一侧改了验收断言（`test_eval_p7_r3.py:347`，从 `"caveat on method" not in` 放宽为 `"or with a caveat on method" not in`），AGENTS.md 规定实现者不能为通过测试改断言，需要审查者确认这处放宽与 D19 一致（当前 PROMPT 把「declared inferred with a caveat on method」列为非因果，`judge.py:158`，看起来符合 D19，但应由审查者判断）。另外分支记录:81「验收测试待独立作者按 r3 重写」已经过时（`42bfffa0` 已有独立作者写的测试），合并时更新。Q7 的「同步 main 后合并」加上 Q10 授权，覆盖 Agent 自行合并 | 合并门 | |
| R10 | 新 validation 来源采集的前置条件 | — | #283/#327（录制在发送前被拒时误判 IDENTITY_MISMATCH）和 #284（缺调查侧 Prometheus 凭据）仍未关闭。上次是靠在子 shell 手工载入凭据绕过的（P7 记录 srcb 轮）。默认沿用这个绕过办法并在开跑前实测 metrics 查询返回 200；#283 造成的槽位损失照旧用换轮次名补跑。采集前和其他线协调 kind lab 使用，并检查 swap（本次预审时 swap 只剩约 0.3–0.5 GB） | 实验室产出 | |
| R11 | 在哪个工作区跑调参 | — | 沿用 `tmp/eval-p7/p7-cal-20261019`。`repo_root` 解析到主 worktree（`eval_p7.py:388-400`），在任何 worktree 里运行都读写主仓库的 `tmp/eval-p7`，`git worktree remove` 不会删掉它。label-pack 与 exposure.jsonl 都已存在（已核对目录） | 数据位置 | |

## 九类覆盖

1. 时间：调参轮之间没有时间要求；formal 前的复核间隔见 R7（可逆）。
2. 计数与口径：J1、J2、J3、J4（待决）；分母沿用 P10（`2026-10-12-eval-suite-contract.md:141`）与 C3:399。
3. 状态转移：调参 → 达标 → 新工作区 → 盲标 → 裁决 → freeze → formal；3 轮不达标 → #312（Q7 已定）；J5、J6 待决。
4. 主体与权限：标注者身份固定（J7）；Agent 合并授权依据 Q10（R9）。只用开发评测凭据，产品权限不变。
5. 数据合同：私有工作区与分诊文件位置见 R6、R11；新工作区见 J6；不涉及数据库迁移。
6. 旧数据兼容：adapter-7 的新字段旧工件解码为 None（`EVAL_P7_R3_AMBIGUITIES.md` 第 1、8 条，已由验收测试覆盖）；旧标签按 location 匹配仍然可用（分支记录:52）。
7. 并发与水位：judge 串行调用（`calibration_flow.py:876`）；kind lab 与本机 swap 见 R10。
8. 外部调用：R4、R5、R7；零重试与 900 s 超时已定（P7-D20、judge-adapters-4）。
9. 验收投影与 `passes`：本步骤不翻任何 `passes`（P7 记录「范围外」）；F1 第 5 步要等新 formal 选定 judge 后才能推进。

## 探针 2

- 草稿补丁：`judge.py` PROMPT 在「Judge each field by its main assertion.」后插入「A summary or claim that restates another field's cause is still its own diagnosis.」（2 行）。
- 结果：`2814 passed, 299 skipped in 498.46s`，**探针 0 失败**。说明没有测试逐字钉住整段 PROMPT，只检查 D17/D19 的关键词（`test_eval_p7_r3.py` 的 D19 断言：「failing dependency is」「confined to」「dominated by」、infer+method、propagat+origin、rejected/ruled out/excluded、so 从句，以及「verbatim」「200」）。调参时如果删掉这些关键词，验收测试会失败，按合同变更处理，不能改测试迁就。
- 共享函数实测：本步骤不改共享函数，没做。

## 收尾

临时 worktree `/Users/shenghuikevin/dev/AI/poa-prereview-judge` 中除草稿补丁外只有 `__pycache__`（`git status --ignored`），已 `git worktree remove --force`，本地分支 `prereview-judge-tmp` 已删除。主仓库已有的用户修改（`docs/tasks/2026-09-27-m1-01-report-contract.md`、`.playwright-mcp/`）没有动；没有发真实模型调用。
