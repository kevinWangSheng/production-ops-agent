# 最终报告失败交接独立复核

基线`e5357a843274879d4b556e88bd3520d7ae1d076f`，PR16评论3977733293。2026-09-10；接续独立审查上下文，未参与实现。亲读GitHub原发现及当前报告分支，并独立用无initial的实际形状fixture验证malformed、empty、length三种失败；load_packet全部抛FINAL_RESPONSE_REQUIRED，发现成立。

根因不局限CLI格式：报告失败借用了初始来源故障builder，需要initial audit才能构造结果，并清空已有artifacts/actions/deliveries。因此修复须独立表达报告失败，复用共同可信业务记录重建，不能只删异常或制造空事故。空原文与无响应保持其真实含义；schema非法或非stop内容只能保留原文审计，不能升级成有效report/完成。

待稳定实现后验收矩阵：无/已验证/未验证initial × malformed/empty/length，实际CLI产生结构化handoff；完整input、原report内容/hash、已有动态证据/动作/物理请求记录保留；旧正常报告及initial unknown分支不退化。仅本地合成业务fixture，0真实模型/后端/PG/trace/凭据读取，旧真实报告与快照不改。

当前为问题复现与验收边界，尚未验证修复通过。

## 最小设计复核

独立同意取消initial专用报告失败旁路，统一共同builder先恢复并核查输入、原始证据、动作和物理请求，再独立判定最终报告资格。malformed/empty/length保留原文与真实执行状态，report=None、明确handoff原因；不能把已有动态证据清空，也不能虚构捕获/committed响应。模型前blocked仍可有真实零delivery。

仅原始`ReportCapture.content`、`Outcome.report_content`从非空Text扩为str，可忠实表示空字符串；这不是接受空报告。有效ModelReport解析仍必须非空、stop、无重复JSON key/DSML。None表示没有原文，空字符串表示实际空响应；其hash分别为None与空字节SHA-256，不能混淆。旧内容寻址schema快照保留，当前候选重新记录schema hash。

**上述最小设计可实施，独立有界设计通过。** 修复通过仍待稳定版本的3×3实际CLI矩阵及无响应/旧合法报告对照，不增加环境或费用授权。

## 稳定候选最终独立复验

固定SHA-256：bridge `5eda13d28d9d273688e6ffcabaffeb00c59929ff520da4c5b4a1ffacabb9ae9b`；outcomes_v4 `15fe3c71db54574b5091f5d98d9ed49092bb5fb24b33f9d6810763de2ab8aeb7`。

亲读统一builder和报告解析/物理响应绑定路径，确认初始专用报告失败旁路已删除。实际响应完整性/身份决定delivery state；报告格式或length失败不伪装为报告完成。报告失败对应blocked/handoff，原HTTP响应可以确已完整返回，两者不混为一个完成维度。

亲自执行：

- `tests/test_m0_holmes_bridge_v4.py -k 'report_failure_matrix or incomplete_response_retains or prior_deliveries'`：11 passed，0.91秒。其中9组真实CLI subprocess覆盖无initial、verified initial、unverified initial分别遇malformed、empty、length；每组退出1且无stderr，--output含完整scenario/outcome/input/原文/hash/违规/交接，stdout不含完整业务对象。
- 六组合同回归：`.venv/bin/python -m pytest tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_initial_evidence.py -q`：184 passed，1.49秒，包含旧合法/unknown路径及绑定拒绝。

本审查者另独立构造同一3×3矩阵，在篡改为失败前先读取原Scenario，然后逐对象比较失败后结果：agent_input、artifacts、observed_actions及deliveries全部相等；不仅检查数量。九组均report=None、execution=blocked、handoff=true，原始字符串及其SHA-256完全相等，strict存在失败结果。无initial的实际已发生动态证据和query action没有被清空。

另独立实际CLI运行无响应None对照：删除response/capture，将已发起请求保留attempt_outcome_unknown；输出仍保留1份artifact及1项action、delivery=dispatched、report_capture=None、report_content=None、hash=None。真实空字符串报告则保留`""`及空字节SHA-256。已有较早unknown delivery与最后完整响应并存的回归保留dispatched/response_committed各自状态，不把未完成请求升级为committed，也不制造一份ModelReport。

当前Scenario schema SHA `0468e2f7805aef189acfeea6196098ed6d5f97dcbed58ac40e6d51e3c19f81a0`、Outcome schema SHA `84ed7977a33b503abd13ea2591ab85dbdba8584038ce0ddc438bb5a00864ad8b`已逐对象核等于DTO导出；旧内容寻址schema快照逐一核文件名hash及e5357a8字节未变。仅原始内容层容许空字符串；有效report解析并未放宽。

**本组稳定候选在上述报告失败交接与审计保真离线范围通过独立审查，无未处理P1/P2发现。** 原P1从无initial三项直接异常变为全部真实CLI结构化失败；初始证据资格与最终报告失败独立表达，不以异常吞掉既有业务记录。

无真实模型、后端、PG、trace或凭据操作；旧真实报告和来源数据不改。本结论不提升自然语言报告质量、不重置预算、不打开M1，最终PR交付仍需最新提交CI及已触发审查闭环。
