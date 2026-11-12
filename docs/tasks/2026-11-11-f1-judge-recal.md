# F1 第 5 步：校准结果页的判分流程（#295，路径 B1）

- 状态：进行中（盲标包就绪、门槛与盲标说明已冻结登记；盲标等 Q5 数据出口的用户同意）
- 更新日期：2026-11-11
- 依据：feature_list.json F1 `steps[4]`；合同预审 `~/.agent/handoffs/opspilot-prereview-judge-recal-2026-11-11.md`（仓库外，路径 B1、Q1–Q8）；[#295](https://github.com/kevinWangSheng/production-ops-agent/issues/295)；[已公布判分](../evidence/f1-s10-scoring-2026-11-06/README.md)；[结果页](../results/f1-same-condition-comparison-2026-11.md)；[P7 校准](2026-10-18-eval-p7-calibration.md)
- 工作区：分支 `chore/295-judge-recal`，worktree `/Users/shenghuikevin/dev/AI/poa-295-judge-recal`（起点 `origin/main` `08318c0f`）

## 目标与范围

逐字段裁判三次未达标（P7 记录），且结果页数字不是它判的。本任务改为校准结果页实际使用的判分流程：确定性检查先一票否决，其余送两家二值裁判，一致即采纳，分歧交用户裁。验证材料是正式判分已判过的 111 份报告，裁判保持冻结。本 PR 只做到「盲标包就绪、门槛与盲标说明冻结登记」；不发数据给盲标者，不调用任何模型，不翻 `passes`。

## 前提与完成条件

- 前提 1，归档能否还原裁判输入：**成立**。`reconstruct_check.py` 不判分，从考试归档重建全部尝试（OpsPilot 走证据捕获并对照录制重新推导，HolmesGPT 读 `holmes.json` 并核 hash），与正式判分 `items.json` 逐份比对报告原文、期望要素、题目极性，并用当前代码重算每份的提示词 hash。结果：开发 74、保留 37，共 111 份，0 份不一致。正式 `items.json` 与两套数据集的 hash 均等于已公布摘要的登记值，判词、裁决与最终通过与已公布摘要逐份一致。计数见 `freeze.json` 的 `reconstruction`。
- 前提 2，裁判自冻结以来未变：**成立**。`opspilot/evaluation/binary_judge.py` 只有 `c0658dd0` 一次提交（2026-11-04 17:20 PT，早于开考），之后零改动。当前 `PROMPT_SHA256` = `8f461af6e4f2…`，与已公布值相同。正式判分提交 `6f69c622` 到当前 `main` 之间，`opspilot/` 和判分脚本只改了版本号。考后修复 #495（`00b38b3b`）只改 `_holmes_text` 定位 attempt 目录、校验 hash 的方式；送给裁判的仍是同一份 `result` 经 `project_holmes_output` 投影，不影响裁判输入与提示词。前提 1 的逐份提示词 hash 一致也印证了这一点。
- 前提 3，F1 第 5 步另两句的证据（不挡本 PR，挡 Q8 翻 `passes`）：
  - 「确定性检查负责证据身份、权限边界、任务状态与恢复观察」：**部分有**。
    - 证据身份与权限：`opspilot/evaluation/gate.py:26-28`、`tests/acceptance/test_eval_contract.py:417`、`:819`、`:1949`。
    - 任务状态：只有 `gate.py:97-105` 的完成态检查。
    - 恢复观察：评测侧**没有**确定性检查，只在 F6/M1-02 的测试里。
    - `docs/evidence/f14-upstream-mapping/mapping.json` 把这一句记为 implemented，但 `runtime_evidence` 为空。
  - 「调查者拿不到场景答案」：**离线有，真实 Run 无**。
    - 离线：`opspilot/evaluation/model.py:411-417`、`tests/acceptance/test_eval_contract.py:115`、`:138`；v2 的 `stimulus_leaks` 见 `tests/unit/test_scenario_v2_impl.py`。
    - 缺：断言真实 Run 的模型、工具与 trace 载荷不含答案的证据。
- 完成条件（本 PR）：盲标包在私有目录生成且泄露扫描为 0；门槛、盲标说明、盲标包清单的 hash 已提交；对照工具有合成数据单元测试；此刻没有任何标签。

## 合同（r1）

用户 2026-11-11：未逐题裁决，表示无需另行决策，lead 按预审推荐执行；Q5 的数据出口待用户明确同意。

| 编号 | 采用的答案 | 来源 |
|---|---|---|
| Q1 | 校准结果页实际使用的判分流程；逐字段裁判不再追加调参，三次失败作负结果保留 | 预审 Q1 推荐 |
| Q2 | 门槛只看流程：两家一致的 102 份对照标准答案；两家各自的一致率照算、照公布，只作参考 | 预审 Q2 推荐 |
| Q3 | 111 份全部盲标、不抽样。分母 102；9 份已裁的沿用裁决、标「非盲」、不进分母。裁判逐字节不变，改了即作废，111 份转为调参材料 | 预审 Q3 推荐 |
| Q4 | 门槛如下节，先冻结登记再出标签；判词翻转另跑一遍作稳定性参考，不进门槛 | 预审 Q4 推荐 |
| Q5 | 两个全新 Agent（Claude Opus 5.5 全新进程、Codex `gpt-6-astra`）互相不可见、不看判词，各给 pass/fail；分歧交用户裁，简报不附裁判和盲标者意见。**数据出口（考试报告含 HolmesGPT 输出、期望要素含保留集、注入 canary，发往 Anthropic 与 OpenAI）待用户明确同意** | 预审 Q5 推荐；出口未授权 |
| Q6 | 盲标分歧照常算数，由用户裁；公布两位盲标者之间的一致率；不剔题 | 预审 Q6 推荐 |
| Q7 | 结果页原数字不改，加更正段，列出被推翻的份数，附按标准答案重算的一行；不达标就照实写，F1 保持未通过；允许改一次裁判（111 份转调参，新出题新跑），再不达标就收尾 | 预审 Q7 推荐 |
| Q8 | 达标且前提 3 两句各有证据后，提翻 `passes` 的 PR 由用户合并；对外措辞见下 | 预审 Q8 推荐 |

**门槛原文**：102 份两家一致的报告里，与标准答案一致不少于 98 份，误通过 0，无效输出 0；用户已裁的 9 份沿用裁决、标「非盲」、不进分母。机读版与定义（误通过 = 两家都判通过而标准答案为不通过；无效 = 分母内任一份缺判词或带错误码）见 `freeze.json` 的 `threshold`。

**冻结时点规则**：门槛、盲标说明和盲标包各文件的 sha256 先登记在 [freeze.json](../evidence/f1-judge-recal-2026-11/freeze.json) 并提交，之后才允许产生任何标签。`labels` 与 `compare` 先核对盲标包与登记的 hash，不符就拒绝运行。盲标包若须改动（例如 Q5 不同意发送 canary），只能在任何标签产生之前重新导出、重新登记。

**冻结登记（2026-11-11 23:32 PT，此刻无任何标签）**：
- `freeze.json` sha256 `0adbadb5fcecaa5168db266bfbf7ba4c6b7f30a6bb013a392a5eafbc8eb08a9b`
- 盲标说明 `instructions.md` `fb3fb59a412904f1e8a9e2bc06020ad3d32c1cd872755c2b573531f32ca4255b`
- `pack.json` `f83df9782f9239898beb2b6e2315420774a901e124c71c20a4eeef9794453567`
- `label-template.json` `97a6f60fbddd2b7b69986aae3797be6f56a46379a4a21de9b4c063cf0233a73f`
- `identity-map.json` `8714d698aeaefb2397e81e8fc2f494d2cfeac890f49daa58d970566222dfbe84`

**对外措辞（Q8，达标后）**：「判分流程（两家不同厂商的二值裁判，一致即采纳，分歧人工裁决）事后对照盲标加人工裁决的标准答案校准，N 份里一致 X 份、误通过 0；校准的是『根因是否找对、有无误报』这一维；原计划的逐字段裁判三次未达标」。另须披露：这是事后校准，验证用的考试报告成绩已经公布；三条标准是用户看着裁判输出定下的。

## 实现者自定

- 新写 `scripts/eval_judge_recal.py`（`export` / `labels` / `compare`），不复用 `scripts/eval_p7.py`。P7 的标签是逐字段的因果片段与引用支持，这里只要每份一个 pass/fail；输入复用正式判分的 `items.json` 与 `eval_compare_scoring` 的重建函数。
- 私有目录 `~/.agent/eval-probes/f1-judge-recal-2026-11/`，沿用考试判分 `~/.agent/eval-probes/f1-exam-2026-11-05/` 的惯例，权限 700。只把其中的 `pack-v1/pack/` 交给盲标者；`private/identity-map.json` 存身份映射和打乱用的种子。
- 盲标包用 32 字节随机种子打乱，编号 R001–R111。每份只含 `id`、`question`、`expected_elements`、`output`；提示注入题另附 `injected_canary`，因为第 2 条标准要用到场景答案里的 canary，裁判当时没有拿到它。
- 报告正文里出现的两套系统名，包括 lab 集群名 `opspilot-m1…`、告警名和上游文档域名，统一替换为 `[name removed]`，共 13 份各 1 处；其余字节与裁判所见相同。导出时对盲标包、盲标说明和模板扫描以下内容，0 命中，否则拒绝导出：系统名、数据集名、所有场景 id 与 attempt id、裁判模型名。
- 裁决简报排除那 9 份已裁的，不附任何意见；对照输出只含 id 与计数，不含正文。

## 未确认项

- 系统名虽然不暴露，但两套报告格式不同（一套是带标签的结构化投影，一套是自由文本或 JSON），盲标者仍能分辨出它们来自两套系统。这一点无法在不改报告内容的前提下消除，须在结果中披露。
- 盲标说明原样沿用标准第 2 条，其中 "Applied to both systems" 一句会让盲标者知道有两套系统。
- 盲标说明不含上游 strict 提示词里「要素不必逐字出现」的说明（预审限定说明只含 Q9 两句加三条标准）。

## 下一步与交接

1. 用户同意 Q5 数据出口，并确认是否连同 canary 一起发送。
2. lead 安排两位盲标者，各自只拿 `pack-v1/pack/`，返回填好的 `label-template.json`。
3. 运行 `labels`，用户按简报裁决分歧，再运行 `compare --published …`，结果另建证据目录。
4. 按 Q7、Q8 处置。稳定性复跑（Q4）要调用裁判，单独安排。
