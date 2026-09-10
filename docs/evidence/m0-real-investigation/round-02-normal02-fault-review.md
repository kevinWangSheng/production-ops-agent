# M0-02 normal02 与后续故障独立结果核验

2026-09-10。独立审查者未参与实现，以新上下文只读检查实际业务工件；没有读取 `.env`、private-protocol、provider reasoning 或 PG 私有 wire，没有调用模型、trace 上传、后端查询或环境操作。

## normal02 结论

**真实最终 JSON 报告已产出，协议/交付引用链通过；正常报告的证据语义质量未通过。** `m002-normal-02` 为4次真实HTTP、15次工具查询，最终 `m002-normal-02-http-11` 已 response_received/HTTP200、stop。实际正文能解析成20项 claims、completed/partial，并明确不认证健康/恢复；这解决本次最终正文缺失，但不等于报告事实正确。旧 normal01 的 DSML/无业务报告失败保持原样。

### 具体发现

1. **P2：把累积 counter 当成授权窗口的增量，进而提出不存在本窗证据的 payment 故障。** Summary、claims 1/3/7/8/13–15/17、gaps 和 next_steps 受影响。`m002-normal-02-e2` 的查询为 `sum by (...) (traces_span_metrics_calls_total{service_name="checkout"})`，e9 为直接 `_count`，e8 为直接 `_total`，e12 为直接 span counter；均返回窗口终点 instant vector，没有 increase/rate/起止差。136、96、payment transactions 10、payment/frontend/proxy/load-generator ERROR 5/6/4/3 是实际返回值，但不能称该5分钟发生的事件计数。尤其 e12 的非零历史 counter 被用来假设“intermittent payment-side failures in the window”，会引导错误的正常窗调查。独立工程同窗 `increase(...[5m])` 原始 calls 显示这四个 ERROR 增量均0；checkout UNSET约139.979、payment UNSET约19.997，transactions约8.748。这些工程值仅作审查反证，没有交给调查模型，也不能替代其主动证据。工程 Prometheus increase 是外推数值，不是整数唯一订单统计。
2. **P2：代理日志把 backend returned 20 和实际可见19行混写。** claim10称“20 returned…all HTTP200 or308”；`m002-normal-02-e7` 明示 returned_hit_count20、displayed_logs19、omitted_returned_hit_count1。可见19行确实只有200/308，不能认证未交付的第20行。应明确19 displayed /20 backend returned /148 total，结论限于可见行。这是可见性合同缺口，虽然未改变本窗主要方向。
3. **P3：截断原因表述不精确。** claim5说449 spans“by the 20-span display limit”，实际462总计、13显示、449省略，是20条和14000字节共同限制，source identity也占空间；gaps已同时提到两种cap，宜保持一致。

不存在 baseline/SLO，报告已正确避免趋势和healthy认证。它正确区分bucket sum与call count、样本span与订单，并披露日志缺失、被拒查询和偏采样；这些优点不消除上方counter语义错误。

### 逐 claim 核验（按 JSON 数组1起编号）

- 1：e2数值136匹配；“in the window”增量解释不成立，见P2。
- 2：e15确实只返回 checkout UNSET136，没有ERROR series；只能说所返回series未显示ERROR，不是完整窗覆盖证明。
- 3：e9 Charge10/GetProduct18/EmptyCart10/GetCart10/GetQuote10/ShipOrder10/Convert28，求和96、status0匹配；都是累积值，本窗计数不成立。
- 4：e3/e4 bucket sums1394/109、status0匹配，正确未称calls。
- 5：e5/e14同一9个trace集合、462 spans、checkout124、max72981µs，raw重算匹配；13显示449省略，偏采样限制保留，截断原因见P3。
- 6：e5/e14的trace `d8a8c1612f3bc272814510a67387fcd0` 确有 CHILD_OF 链：load-generator fa6c2ca5a924fa96 → proxy f0154d1c2b662791 → proxy0d5e68a2fe04b581 → frontend b0d519d4d7a4d2c3 → frontend06e598c3d8241212 → frontend aa0456e92d1b84be → frontend439f9f8986edb1d0 → checkout9e4104d8b0d0a7d8。模型合并了同服务中间步骤，方向和200/0标签正确；checkout72981µs正确。服务身份是telemetry实际service，部分container mapping仍unknown；报告没有把未知实例编成已映射。
- 7：e12列值payment ERROR5/UNSET24、frontend6/740、proxy4/354、load-generator3/173匹配，时间含义有P2。
- 8：e8 payment10匹配且未说unique orders，但仍非窗口增量。
- 9：e6 backend75、returned/displayed20，均Information，时间02:41:18.619–02:43:25.401匹配，结论应限样本。
- 10：e7 backend148、returned20、displayed19，实际可见02:43:00.789–02:43:25.366均200/308，POST checkout200匹配；见P2可见行数。
- 11：最终请求仅含cart/proxy日志；初始业务输入available_log_sources也仅两者，无checkout日志事实成立。
- 12：e10/e11/e13均实际 error `returned service outside scope`、data_withheld true，三个查询描述匹配。
- 13：payment本窗intermittent错误假设基础受counter时间误读污染，未通过。
- 14：明确标为未验证假说，无工程注入答案泄漏证据；但源counter不支持本窗错误，UNSET也不应直接等同success-path。
- 15：样本成功链/200日志有依据；“all96 observed calls”需改成累积series而非本窗计数；无ERROR返回不能排尽缺测。
- 16：无baseline、未声明趋势正确；bucket sum并非duration figure，72981µs确为样本上界。
- 17：限定了已观察证据，但“rejected checkout errors during window”仍偏强；有限样本与counter空series只能说未观察到支持证据。
- 18：人工建议合理，但须先查窗口增量，不能以历史5/6/4/3当本窗错误。
- 19：checkout日志源缺失由初始发现支持，建议为人工参考，无执行行为。
- 20：缩小service selector重试为合理人工建议，不保证服务范围拒绝一定能解除，也不授权新增请求。

### 独立核验记录

工件根为环境worktree `tmp/m0-environment/holmes-runs/m002-normal-02/`。最后delivery是http11，而非任意历史union：已逐一核15份manifest的raw/view文件SHA256及canonical JSON SHA256；与最后delivery登记view_sha256一致，并在最后真实业务message快照中各找到一次完全相同的紧凑JSON view。claims所有完整evidence_id均在最后请求中；`final_business_content`解析结果与final_report相同。e5/e14 raw独立重算9/462/124/72981，与view匹配。工程observation各raw_path文件hash与登记一致。

- result-business.json SHA256 `f718dc6b1ca19e0c7597dae67ac373961278ad091355673eb13f376deba3f908`
- delivered-business.json SHA256 `81c02c2261579690f729af89784463f4b12bca8a1d4ea1c3869990e56b14036c`
- 最后messages canonical SHA256 `629297a2f4ce7b8b30c7be8c17a0acaa54a647dc87a167807c2f430e1245a4e9`
- 工程依据：`docs/evidence/m0-real-environment/m002-normal-01-observation.json`，窗1789007908..1789008208；其raw-evidence同label保持。

本核验以可信运行器的response_received交付登记与业务快照作为实际发送证据；不读取私有wire，不声称独立网络抓包。没有把程序investigation_returned当质量证明。共享宿主不等于OS隔离或盲测；这是一次开发案例，不打开SPEC/M0/M1整体入口。

## 后续故障

尚未收到新故障工程窗及对应模型Run；故障可诊断性和最终报告核验待补充，不能以本正常报告替代。
