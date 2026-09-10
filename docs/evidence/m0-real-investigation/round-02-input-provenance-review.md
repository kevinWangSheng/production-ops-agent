# 输入来源元数据严格要求独立复验

2026-09-10，基线96e89e3，评论3977946891。独立审查者接续此前审查上下文，未参与实现。基线亲自验证verified initial下删除provenance、改为空对象、同时删除provenance与question-original三项，旧strict均错误返回无违规；问题成立。

## 固定候选与代码核查

- holmes_bridge.py SHA-256：beb8393a29a597d61286b85b51d33ea299f2463db649d1e8588038f30f9b9b72
- outcomes_v4.py SHA-256：25e89209e92a62e3913ebe590df4747f6ab4fc469d6c60d0fc86d8c703c9f4bf

bridge已去掉truthy guard，缺失/无法解码/非对象/缺hash/不匹配及原question缺失都形成input-provenance审计违规，不丢弃整个packet。共享input_provenance_errors对原/实际内容及hash分别要求存在并一致，同时用于最高checker及investigator_input导出；不因双方None而通过。DTO可保留None用于真实缺失审计，schema与旧v3语义不需放宽。

## 亲跑证据

`.venv/bin/python -m pytest tests/test_m0_holmes_bridge_v4.py -k provenance_is_required -q`：18 passed，1.94秒。两个模式（正常动态/已验证初始导入）分别覆盖valid、missing、empty object、empty file、malformed、non-mapping、missing original、missing hash、wrong hash；每项运行实际CLI subprocess。正确来源仍通过，其余返回结构化违规，不是generic异常。

另由本审查者独立构造6项实际CLI：正常动态/初始导入分别遇JSON null、非法UTF-8、provenance与original同时缺失。全部退出1、stderr为空，具有明确UNVERIFIED_INITIAL_EVIDENCE；失败前后逐对象比较artifacts、observed_actions、deliveries及整个Outcome完全相等，完整原报告和actual input保留。--output完整scenario/outcome可审计，stdout不含scenario/outcome/agent_input/report_content。

另直接清空Scenario中original/actual两组content/hash：checker返回INITIAL_INPUT_PROVENANCE_UNKNOWN，investigator_input明确拒绝导出，堵住绕开bridge的空值对照。

完整六组回归：`.venv/bin/python -m pytest tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_initial_evidence.py -q`：205 passed，3.24秒。没有依据实现者摘要宣布通过。

## 结论

**固定候选在输入来源缺失/非法处理、直接checker与真实CLI的上述离线范围通过独立审查，本组无未处理P1/P2发现。** 缺失元数据仍保持真实缺失并违规，不补造原question或把候选报告质量改为失败格式；报告、证据和动作记录保留，严格资格独立判断。

只使用本地合成fixture，未进行模型、网络、环境、PG、后端或真实凭据/private操作。旧真实报告与源码/schema快照不改，原自然语言质量FAIL及M1入口状态不变；PR最终交付仍需最新提交CI与已触发审查闭环。
