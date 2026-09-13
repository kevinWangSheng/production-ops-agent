# PR16 reportless公共不变量修复

基线`f923897`，comment `3978118760`。本组只改v4 checker与相关测试；bridge/runtime/schema/旧真实工件/PG不改，没有模型、网络、环境操作或提交。root负责任务/结果/PR，独立reviewer另维护复验记录。

## 同组核查与红证据

原checker在report=None时提前返回，实际input是否出现在持久delivery只在已匹配的最终有效报告分支检查。prepared、dispatched、response_committed三种状态中缺input或替换input均可错报contract-consistent handoff。审查early return下全部校验还发现：已有capture的身份/代次/请求、原文/hash与对应delivery的时间一致性也不依赖报告解析成功，却同样被跳过。

新增回归先红：

```sh
.venv/bin/python -m pytest tests/test_m0_outcomes_v4.py -k reportless -q --tb=short
```

`10 failed, 4 passed, 18 deselected in 0.13s`：6个缺/替换input反例错误[]；capture wronghash/request/time和outcome rawhash四例只有MISSING_REPORT_OR_HANDOFF，遗漏对应绑定违规。3个合法payload和无delivery handoff原本通过。最初合成reportless fixture错误保留了报告级evidence_ids却移除报告身份，引起额外EVIDENCE_NOT_VISIBLE；修正合成fixture不宣称报告引用后得到上述明确红证据，没有修改该证据可见性规则。

## 最小修复

- 将actual input绑定放到每条持久delivery的共同循环；prepared只表示持久payload需匹配，不被当作已发送。envelope及Holmes user/tool业务messages保持各自既有结构检查。
- raw content/hash有记录即核配对；capture实际存在时，条件核run/step/request/generation、原文/hash、恰一匹配committed delivery及capture/response收取时间一致性。
- 原有context、control、scope、hash及delivery时间顺序检查仍在共同路径。无capture/无delivery不新增成功响应时钟要求，合法handoff继续通过。
- 报告解析、完整有效报告必需时钟和claims目标/新鲜度检查仍以有效report存在为前提；非法raw报告仍原样审计且不能被升级成有效报告。

## 作者结果与冻结

```sh
.venv/bin/python -m pytest tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_initial_evidence.py -q
.venv/bin/ruff check scripts/m0/outcomes_v4.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge_v4.py
```

最终`233 passed in 3.30s`，ruff通过。两套矩阵覆盖envelope与实际Holmes业务messages的prepared/dispatched/committed × valid/missing/wrong；另覆盖无delivery合法handoff、capture wronghash/request/gen/time、缺clock、非committed及合法raw审计。无传输调用，均隔离替身工件或DTO。

交独立复验的稳定SHA256：

- outcomes_v4.py：`955cdc0b840343fb9e742d63b2fb3a36c6f7bb2446ba9dcb74fb67375f81071f`
- tests/test_m0_outcomes_v4.py：`f545f1999b55ea1a69b41fbb180587049741a513d2189fc8ffbfe495a8c27552`
- tests/test_m0_holmes_bridge_v4.py：`a31963071dfefc7ccdfbeb2b1b85ada823822116e0a8edc4850bd7f5db0bccfe`

以上为实现者自测，不自称独立通过；不改变历史qualityFAIL或开启新真实实验。
