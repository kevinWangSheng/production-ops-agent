# 报告解析无关的输入与捕获绑定独立审查

2026-09-10，基线f923897，发现3978118760。独立审查者接续同一上下文，未参与实现，无模型、网络、环境、PG或凭据操作。

直接检查`check_outcome`早退上下游：实际user输入成员关系仅在report存在并匹配committed delivery后校验，prepared/dispatched及无report失败交接绕过；此外capture身份、content/hash及对应时间绑定也位于早退后。这些是持久输入/捕获审计真实性，不能依赖报告解析成功。

另独立构造report=None、execution=blocked、合法handoff但保留并篡改capture.content_sha256的Scenario；旧checker实际返回[]，同组遗漏得到复现。

最小修复边界已与作者对齐：每个持久delivery验证输入绑定（prepared只是准备记录，不宣称已送达）；存在capture时条件式核身份、hash及对应delivery时钟；无capture/无发起仍可表示合法handoff，不凭空要求完整响应。只有报告parser、唯一完成报告匹配、事实来源/时效依赖有效report存在。

待稳定代码实际复验阶段矩阵与合法无delivery交接后追加结论，不预设通过。

## 稳定候选独立终验

固定`outcomes_v4.py` SHA-256：`955cdc0b840343fb9e742d63b2fb3a36c6f7bb2446ba9dcb74fb67375f81071f`；bridge/schema本组未修改。

源码逐段复核：actual input成员关系现对每一条持久delivery执行；context、身份/版本、人控、已知时钟顺序、raw内容/hash以及存在capture时的身份/hash/唯一committed delivery/接收时间相等检查均先于report=None返回。早退后仅保留有效报告解析、完成报告必需时钟、结构化claim及事实时效等报告相关条件。无capture不凭空创建捕获，无delivery不要求实际发送；prepared只校验准备记录的payload，不宣称已交付。

亲跑`tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge_v4.py -k reportless`：31 passed，0.54秒。覆盖envelope及实际Holmes业务消息两套prepared/dispatched/committed × valid/missing/wrong矩阵、无delivery合法handoff、capture错hash/request/generation/time、缺时钟、非committed及合法原文审计。

本审查者另独立构造12项多delivery反例：有/无有效report × 前一条delivery的三阶段 × missing/wrong input，最后delivery保持正确。全部明确ACTUAL_INITIAL_INPUT_NOT_DELIVERED，证明不能只验最后一条成功请求。

另独立运行9项实际CLI subprocess（prepared/dispatched/response_committed × valid/missing/wrong）：准备/已发起使用真实无响应None，完整返回使用空报告失败。全部形成完整结构化失败packet、无stderr；missing/wrong均有ACTUAL_INITIAL_INPUT_NOT_DELIVERED，valid不会错误产生该违规。保存的scenario仍有原artifact/action/delivery各1项，actual输入原文保持，stdout不含scenario/outcome/input/raw全文。合法输入下报告自身失败仍保留，未通过改变report状态制造通过。

六组回归亲跑：`.venv/bin/python -m pytest tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_initial_evidence.py -q`，233 passed，3.19秒。

**上述固定候选在报告无关输入/捕获绑定及失败交接的离线范围通过独立审查，本组无未处理P1/P2发现。** 实际输入缺失/替换和reportless capture篡改均不再因早退跳过；无delivery、无capture的合法交接仍可表示。

未运行模型、网络、环境、PG、后端或凭据操作；原报告质量、预算与M1门槛不变。最终PR仍须覆盖最新提交CI与已触发审查结果。
