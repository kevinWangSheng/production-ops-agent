# F1 第 5 步判分流程校准：结果（#295，路径 B1，2026-11-24）

结论：**未达成**。冻结门槛是 102 份两家裁判一致的报告里与标准答案一致不少于 98 份、误通过 0、无效 0。实际一致 95–96 份、误通过 4–5 份、无效 0。区间来自尚未由用户裁决的 1 份盲标分歧：它判 pass 或 fail 都改变不了结论，所以本次不等这份裁决就收尾（R4）。

## 盲标者

- R3 原定 Claude Opus 5.5 加 OpenAI `gpt-6.2-astra`。实施会话的进程环境里没有 OpenAI judge 密钥（按 `opspilot/evaluation/binary_judge.py` 的取值方式读 `OPENAI_API_KEY`，结果为空），按 R3 改用 **Claude Opus 5.5（`claude-opus-5-5`）与 Claude Fable 5.2（`claude-fable-5-2`）**。**两位盲标者来自同一厂商（Anthropic）**，独立性比原计划的两家异厂商弱。
- 执行方式见 [label_driver.py](label_driver.py)：每位盲标者每份报告各起一个全新的 `claude -p` 进程（共 222 个），只给冻结的 `instructions.md` 和盲标包中的那一份；不开工具、不连 MCP、不保存会话、空工作目录、替换系统提示词。盲标包约 1.4 MB，一个上下文放不下，且说明要求逐份独立判断。互相看不到对方结果，也看不到裁判判词。
- 模型核对：每个进程返回的 `modelUsage` 都含所指定的模型。Fable 111/111 只有 `claude-fable-5-2`。Opus 110/111 只有 `claude-opus-5-5`；其余 1 份的 `modelUsage` 除 `claude-opus-5-5` 外还记了 `claude-fable-5-2`（推测是 Claude Code 的辅助调用，未确认）。出标签前按事先定的规则重跑一次，两次标签相同，`modelUsage` 相同，采用重跑结果。
- 费用：`claude -p` 自报的估算值约为 Opus 10.6 USD、Fable 35.8 USD。走本机 Claude 订阅，不是 API 账单。

## 数字

| 项 | 值 |
|---|---|
| 两位盲标者一致 | 109 / 111 |
| 分歧 | 2 份：1 份属于已裁的 9 份（沿用用户原裁决），1 份待用户裁决 |
| 流程分母 | 102 |
| 与标准答案一致 | 95–96（门槛 ≥ 98） |
| 误通过（两家裁判都判通过，标准答案为不通过） | 4–5（门槛 0），全部是 OpsPilot 的尝试 |
| 误判不通过 | 2 |
| 无效 | 0 |
| 单家裁判对照标准答案（111 份，仅参考） | GLM 97–98 一致、误通过 11–12；OpenAI 102–103 一致、误通过 4–5 |
| 已公布结论被推翻 | 6–7 份 |

区间左值是待裁那份判 fail，右值是判 pass。

按标准答案重算的单次尝试通过数（已公布 → 重算；未经裁判的尝试保留已公布结果）：

| 数据集 | 系统 | 已公布 | 重算 |
|---|---|---|---|
| 开发集 | OpsPilot | 50 / 54 | 46 / 54 |
| 开发集 | HolmesGPT | 31 / 42 | 31 / 42 |
| 保留集 | OpsPilot | 27 / 36 | 28–29 / 36 |
| 保留集 | HolmesGPT | 15 / 24 | 15 / 24 |

## F1 第 5 步

| 子句 | 状态 | 依据 |
|---|---|---|
| 确定性检查负责证据身份、权限边界、任务状态与恢复观察 | 达成（恢复观察按 R5 由 F6 承担） | 证据身份与权限、任务状态：`opspilot/evaluation/gate.py`；恢复观察：F6（`passes: true`）的[真实运行证据](../m1-02-live-2/)、[离线重放](../m1-02-replay/)与 [F6 测试映射](../../testing/f6-acceptance-tests.md) |
| 用人工复核校准报告质量判分 | **未达成** | 本页 |
| 调查者拿不到场景答案 | 达成（有界：89 个有录制的真实 Run） | [考试真实 Run 扫描](answer-isolation-scan.md) |

F1 `passes` 维持 false。按 R4 不改裁判、不换新题重跑，#295 收尾。

## hash（文件都在仓库外私有目录 `~/.agent/eval-probes/f1-judge-recal-2026-11/`）

- 冻结文件 `freeze.json`：与登记值一致（`labels`、`compare` 运行前已核对）
- Opus 标签 `labels-claude-opus-5-5.json`：`0016dc45bda39fd12c58248a9c95ee0f0d6be14e33c2cb65708e6787b59ae19e`
- Fable 标签 `labels-claude-fable-5-2.json`：`76ca5fd0d74ddddde6956b86a1b05c7df88d9edad278c7195be9b5a10d1e697d`
- `labels-v1/merged.json`：`8fa029374167cfd43f54db808174147bbb70624f14ba2ee8f930a78fa379dc8a`
- `labels-v1/adjudication.json`（未填）：`94178666f5cfa1e4f7f46736e1bf10ca95065be53cf9ba673939d9f9cd891cbd`
- 两种假设下的 `compare` 输出：待裁判 pass `eec17bb0ce0b94fe71ef8ec61a3e481c166e72ecf76a4c5bcae98469af88e0a9`，判 fail `f9f91d2ba52351fe3a965084c99c845a1317bfb10dd4c506a0a59bae8f5f9b35`

## 限制

- 两位盲标者同厂商；标准答案里 101 份只来自这两位模型盲标，没有人工复核（Q6 只让用户裁分歧）。
- 盲标者能从报告格式分辨出两套系统（任务记录「未确认项」）。
- 这是事后校准：被校准的考试成绩已经公布，三条标准是用户看过裁判输出后定的。
