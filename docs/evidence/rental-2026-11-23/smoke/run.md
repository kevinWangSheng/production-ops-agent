# 批 4 租机冒烟（2026-11-23 16:06–16:23 UTC，08:06–08:23 PT）

性质：有界真实运行，不计入分数。冻结提交 F `51257e79`，运行 checkout 无改动。#666 驱动 `round --trials 1 --trace lab`，judge 开启；用真实 ES `cp1-es`（127.0.0.1:30920），产品只读 key；K8s 用 kind lab 的只读身份。命令在主机 `/home/opspilot/rental-logs/b4/{smoke,o_round}.sh`。两个时段错开：先跑 app-162 的 K8s 夹具题，跑完后再跑 ES 题。没有改提示词或产品代码。

## 162：不带与带常驻说明（B4-U3 c，R2-3）

带说明的那次用评测侧包装 `/home/opspilot/rental-logs/b4/gi_driver.py`。它把上游 `global_instructions.json` 的 5 条说明，按上游 `base_user_prompt` 的 "Global Instructions" 格式（`* 条目`）拼在问题后面再提交。驱动、产品代码和题目文件都没有改。两次的 question sha256 不同（`5bbaf3b3…` 对 `125acd48…`），说明确实拼接了。两次都带披露码 `GLOBAL_INSTRUCTIONS_NOT_IMPORTED`，这是驱动的固定披露码。

| 方式 | Run | 状态与模型请求 | judge | 报告是否回应说明 |
|---|---|---|---|---|
| 原题（不带说明） | `cbbc6c05-eddf-5a8d-84d9-bc17396e2d8a` | completed，10 次 | B | 没有回应。报告没有提到说明里要求的升级对象 |
| 拼上常驻说明 | `ff8ce3e7-4cb1-5bd4-b05a-9495242580c1` | completed，12 次 | A | 回应了。报告按说明给出了升级对象，并按"先查依赖"的顺序展开 |

两次的排障结论相同，都定位到同一个依赖服务和同一个配置错误，差别只在说明要求的升级对象。判定：成立。162 失分的原因是缺少常驻说明的输入通道（#780）。各只有 1 遍，属于弱证据。按 R2-3 只记录，不开 issue。

trace：原题 https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/16e4d27a-a7ae-4ef5-889d-742661ae53b3/r/00000000-0000-0000-3c8b-31d71bfb6284；带说明 https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/508dad19-0f7f-4127-b732-d37f151eba3b/r/00000000-0000-0000-c3ae-744f187c3794。

## ES 时段：185、280、235，对照 254、275

| 题 | Run | 状态与模型请求 | judge | time_box_limited | prompt / 命中 / completion |
|---|---|---|---|---|---|
| 185 | `37f4b5eb-1019-535f-b1da-24ff0f76c892` | completed，18 次 | A | false | 698,226 / 609,024 / 21,605 |
| 280 | `0236a040-31e3-5ca0-b337-41428b62f00d` | completed，7 次 | A | false | 177,632 / 138,752 / 13,017 |
| 235 | `27d88d61-cf6c-5724-bbed-e32907bea5fb` | completed，26 次 | A | **true** | 1,261,443 / 1,199,232 / 23,958 |
| 254（对照） | `c2e458c3-19df-52f5-aced-04219ec85798` | completed，8 次 | A | false | 171,317 / 148,480 / 11,059 |
| 275（对照） | `fe19c4d1-4429-5973-a533-6801eb4cefc2` | completed，4 次 | A | false | 76,847 / 72,320 / 2,598 |

- 每个 Run 的授权窗口都是 30 天，起点是提交时刻往前 30 天，说明驱动提交时带了最大回看窗口（#809）。185 和 280 此前因 24 小时窗口失分（#779、#774），这次都是 A。
- 时间盒实况：235 的 summary 里 `time_box_limited: true`，这是首次在真实 Run 里看到这条路径。它的 shift 没有生效（`applied: false`），Run 本身是 completed / A。
- 162 两遍的 tokens：原题 285,838 / 259,328 / 14,517，带说明 348,150 / 324,224 / 10,206。冒烟 7 个 Run 合计 prompt 3,019,453（命中 2,751,360），completion 96,960。
- 证据：`holmes-evals-smoke-*/<题>/trial-1/summary.json`。
