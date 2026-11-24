# 批 4 全量（Holmes 题集 OpsPilot 侧 + #808 新增 4 题 Holmes 侧；SREGym 见 sregym/run.md）

性质：有界真实运行（opspilot-tool-1，冻结提交 F `51257e79`，运行 checkout 无改动），DeepSeek 真调，judge glm-5.3-flashx（opspilot 口径），`--trace lab`。不是生产证明。没有改提示词或产品代码。

## 跑法

- 命令在主机 `/home/opspilot/rental-logs/b4/{full,o_round,h_round,finish}.sh`，不入库。两条线 B（端口 30000+）和 S（35000+），都在运行 checkout 里跑。ES 是两个自起的 ES 8.19.25，与 CI 同一 digest，开启安全认证：`cp1-es` 在 127.0.0.1:30920，`cp1-es2` 在 30921。产品只读 key 由 `scripts/es_readonly_identity.py` 预先建好，经 `--es-readonly-env` 传入。工程侧 key 通过环境变量传给驱动；Holmes 侧的 env 文件用进程替换现场拼出，不落盘。
- 三个阶段（16:23–19:41 UTC，08:23–11:41 PT）：
  1. C2（app-NNN 夹具题）与不带 K8s 的 C5 并行。
  2. B1 分两半，各用一个 ES。
  3. 带 K8s 的 C5、C1、283 关 TodoWrite、todo-hard 排一条线，C6 排另一条线，最后是 Holmes 侧 4 题。
  这样 app-NNN 夹具题与 B1（含多集群题）不会同时在集群上，避免 11-20 出现的 162 夹具污染 235；C5 的两组也不重叠。
- 不带 `--k8s-env-file` 的只有上游禁用 K8s 的 259_loki、257、258，与 11-20 相同。

## 结果（主集合：57 题 × 3 = 171 遍）

| 组 | 本次 | 11-20 全量 | Holmes（11-20，新增 4 题为本次） |
|---|---|---|---|
| B1（34 题，含新增 4 题） | 99/102 | 72/90（30 题） | 86/90 + 新增 12/12 |
| C5（9 题） | 26/27 | 27/27 | 27/27 |
| C2（6 题） | 15/18 | 10/18 | 18/18 |
| C1（283） | 2/3 | 3/3 | 3/3 |
| C6（7 题） | 21/21 | 21/21 | 16/21 |
| **合计** | **163/171（95.3%）** | **133/159（83.6%）** | **162/171（94.7%）**；原 53 题 150/159 |

- 只看 11-20 的同一 53 题：OpsPilot 151/159，11-20 为 133/159，Holmes 为 150/159。
- 各遍明细：各轮 `opspilot/holmes-evals-full-o-*/scores.json`，Holmes 侧在 `holmes/holmes-compare-full-h-b1new/scores.json`。trace 链接在各 summary.json 的 `trace.roots`。
- Holmes 侧只补跑了 #808 的 4 题（183a、183f、183g、186，各 3 遍，12/12）。其余 53 题沿用 2026-11-20 的 Holmes 侧结果（`docs/evidence/rental-2026-11-20/full/holmes/`，judge 口径与模型相同），本次没有重跑。

### 没过的 8 遍

| 遍 | 情况 |
|---|---|
| 162 t1–t3 | judge B。原因与冒烟验证一致：缺少常驻说明（#780），见 [冒烟](../smoke/run.md) |
| 245 t3、261 t2 | judge B，属于摆动题；11-20 两题都是 2/3 |
| 283 t3 | judge B，摆动；11-20 为 3/3，11-22 重跑 1/3 |
| 275 t1 | 没有报告：产品转人工（waiting_human，INCOMPLETE_INVESTIGATION，模型自标 incomplete/inconclusive） |
| 100a t1 | 没有报告：REPORT_INVALID。终答两次都是不合法的 JSON：完整对象之后还有多余字符，`json.loads` 报 Extra data，位置在第 17544 列附近。#825 的重试反馈也没能纠正，属于模型输出问题 |

### 与前两次相比

| 题 | 本次 | 11-20 | 11-22 重跑 | 可见原因 |
|---|---|---|---|---|
| 185 | 3/3 | 0/3 | 0/3 | 驱动按 #779/#809 提交 30 天回看窗口，此前因 24 小时窗口失分 |
| 280 | 3/3 | 0/3 | — | 同上 |
| 259_wrong、264、266 | 各 3/3 | 各 0/3 | 各 3/3 | #776 报告合同的完成边界（11-22 已验证） |
| 235 | 3/3 | 2/3 | 1/3，隔离重跑 3/3 | 按阶段隔离，没有和 app-NNN 夹具并行 |
| 89、90、94 | 各 3/3 | 2、1、1 /3 | 2、2、2 /3 | #772 runbook 点名 |
| 183a/183f/183g/186 | 各 3/3 | 未跑 | 未跑 | #808 新增工具 |
| 100a | 2/3 | 3/3 | 0/1，追加 3/3 | t1 REPORT_INVALID（见上） |
| 275 | 2/3 | 3/3 | 1/3 | t1 转人工 |
| 283 | 2/3 | 3/3 | 1/3 | 摆动 |

### C1-U2 对照（不计入主集合）

- todo-hard（184/185/193/280，TodoWrite 开）：11/12。185 t3 转人工（INCOMPLETE_INVESTIGATION）。11-20 为 5/12。
- 283 关 TodoWrite：0/3，与 11-20 相同。

## 异常与披露

- **judge 余额耗尽**：Z.ai 从 18:00Z 起返回 HTTP 429 code 1113（Insufficient balance）。70 遍当时没有判分：主集合 56 遍，对照 14 遍。产品 Run、报告和 summary 不受影响。余额恢复后（20:44Z），用 `driver judge` 按保存的答案文本补判，模型和口径不变，补判全部成功。这些 Run 的 summary 冻结在补判之前，补判结果按驱动规则写回同一 summary。
- **Holmes 侧 4 题推迟**：Holmes 侧 runner 不能补判，写出的 summary 就是最终结果。所以 judge 不可用时推迟了这一步：已启动的那一次在 30 秒内按精确 PID 停掉，没有写出 summary，也没有留下 ES 索引。余额恢复后完整重跑，20:50–20:56Z。
- 驱动层没有中止，也没有 env_broken。几轮退出码为 1，都只因为当时有 judge_exhausted 的遍。
- 提交给产品的都是 30 天回看窗口（#809）；`time_box_limited: true` 的有 184 共 6 遍（B1 3 遍、todo-hard 3 遍）和 235 共 3 遍。

## 用量

DeepSeek（OpsPilot 侧 186 遍，含两组对照）：prompt 55,071,621（命中 50,412,160），completion 1,764,066。Holmes 侧 4 题的 token 没有记录（runner 不记）。judge 用量没有统计。
