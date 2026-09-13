# PR16 final指针、初始证据与版本绑定修复

基线`6eab0a4`；comments `3976875093/3976875099/3976875104`。本组有界设计已由fresh reviewer通过。0模型/trace；旧真实记录、旧schema内容快照不改；不提交/推送。

## 3976875093：当前final指针

StepStore.control(cancel/correct)原来只改generation/state，已发布final仍留在主体，summary.published仍true。作者真实PG两分支先红：`2 failed, 24 deselected in 0.80s`。仅在同一控制UPDATE增加`final=NULL`，不动m0_v3_report历史表；同组new_run原已清final。修后定向`2 passed, 24 deselected in 0.44s`，检查旧report整行（内容/代次/时间）不变、旧fence不能重发布、新Run可接续且初始published=false。

命令：`M0_STEP_POSTGRES=1 /Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/integration/test_m0_step_store_postgres.py -k control_clears_active_final -q`。PG由root启动与停止，作者只操作随机替身实验行；没有真实模型或实际CNY消耗。

稳定store SHA：`9e865752d0d4acfc0dcd7c41e81175e944cbc99b0f87d199cbd5cd52c1aed599`。独立验证已由另一Agent完成，证据由其维护。

## 初始证据与版本：获审实施边界

report-only允许没有动态observations；初始views只从可信CLI manifest原raw/view/manifest/原ProjectionContext/Timing验证，精确拷贝到新Run，不移动/重签旧文件。当前仅支持单一兼容context；异registry/source/deps/revision或缺provenance明确unknown并保全实际报告/原始和assembled user input。未验证view按稳定位置保留原content/hash，不强制其ID先合法，以免异常丢失报告。

原user content/hash与实际送模user content/hash分开，checker只拿后者绑定实际physical payload。旧v4 schema内容快照不改，当前未合并候选添加可选审计字段后记录新hash。版本向量将显式包含upstream_commit和tool_schema hash；strict缺失须违例，不用unknown字符串通过。其余实施/验证进展待本组稳定后追加。

## 稳定候选与作者验证

bridge将原始输入与当前scope构造提取为共享小函数，report-only单步可读取缺失的动态observations为[]；动态模式缺文件仍明确失败。使用runtime作者的纯`verify_initial_entry/context_signature`核原文件、原manifest、原source/deps/registry及projection revision；报告中初始引用从实际顶层user business_tool_views提取，不递归扫描。导入记录不生成query action。元数据不足按位置保留输入及原因，不把view当raw。

strict v4增加可选输入审计字段：原user content/hash、实际assembled user content/hash、unverified_initial_views(location/content/hash/reason)。实际内容必须在最终物理请求业务messages中精确出现；原CRLF bytes不归一化。当前v3 DTO/schema无变更；其checker只增加默认空的私有`_initial_view_ids`参数，让v4传入已验证初始ID，显式旧v3默认行为不变。旧内容快照未修改，当前共享源已变，必须由root另存新candidate source manifest。

输出解析与事实资格分开：有完整capture/response绑定且schema合法时，result缺少final_report副本不再使报告丢失；存在副本仍逐字段核对。缺/坏初始来源、有fact的报告可进入strict checker，保留summary/next_steps/claims/原content，同时返回UNVERIFIED_INITIAL_EVIDENCE等违规。模型前因import失败退出则保留原/实际输入，以blocked/handoff输出且无Artifact/Delivery。初始来源有缺陷且报告schema非法时，仅保留原文/hash作为未认证审计，report=None且strict失败；不补空字段、不声称响应被采纳。

版本向量读取当前configuration.upstream_commit与canonical tool_schema SHA256；其缺失/格式未知在strict checker形成EXECUTION_VERSION_UNKNOWN，改变任一实际配置会改变版本向量。原真实报告、旧版本source及qualityFAIL没有重签或改判。

作者命令：

```sh
.venv/bin/python -m pytest tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_initial_evidence.py -q
.venv/bin/ruff check scripts/m0/holmes_bridge.py scripts/m0/outcomes_v3.py scripts/m0/outcomes_v4.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_outcomes_v4.py
HOLMES_TEST_UPSTREAM=/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4 /Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmes-venv/bin/python tests/fixtures/m0_environment/initial_report_probe.py
```

结果：160 passed in 0.35s；ruff通过。真实Holmes代码经fake pipe执行report-only 1step、0工具、0真实HTTP，导入1初始view，strict checker=[]。新定向回归还覆盖missing provenance、坏hash、非法/重复ID、混合context、有fact且无parsed副本、parsed副本不符、实际payload未交付、无模型import blocked及非法报告保真。原finding的独立失败证据见本组设计/独立审查记录；过程中重构引入window局部变量遗漏导致6 fail，修正为共享access.window后上述完整集通过，不把这个调试失败冒充原finding复现。

冻结SHA256（交独立复验，作者结果不自称独立通过）：

- holmes_bridge.py: `cd975a33d33b1dc3904448d15a220368d28f2ebc3f860d4a64843037076861d6`
- outcomes_v3.py: `3e0a82dd1abc53e932efb2bc4ff994a2b92142415d24ba9c2c576a8fb16dac57`
- outcomes_v4.py: `79f9fae88414c27453beab44d27319bfac634e25ac8c1b2baf58e22464a0c404`
- tests/test_m0_holmes_bridge_v4.py: `440ac782ef5224bd75ee1b405be9f373ee80251dcdfe1135fe168d60a867b73c`
- 当前IncidentScenario.v4.schema.json: `3de162f314b72df5f5a9c16d6493b036b0aa593121f4e1f691789b51ba9a4238`
- IncidentOutcome.v4.schema.json: `e019fee6a355f2c586389c991a2887bf37bfc1caec9fcc538eed852bf014b53a`

本组没有新增真实模型、trace或费用，PG由root已停止；没有提交、推送或修改旧真实行。完整原业务输出继续仅留ignored本地工件；本文只列结构、命令与hash。独立复验结果由fresh reviewer单独维护。

### 协议解析补项后的最终冻结

上面的160项和cd975a/79f9fa快照为本组先前候选，已由以下最终快照替代。root/fresh要求共同核输出解析协议：bridge在接纳schema候选前复用已有`report_contract.parse_report`，明确拒duplicate JSON keys、DSML、非法结构，并要求finish_reason=stop；v4完整输出checker也复用同一parser，避免直接DTO seam通过JSON last-wins。缺初始来源且协议不合格仍保存原文字节/hash为未认证blocked审计，不能构造有效Report。新增3种bridge协议反例及1直接checker duplicate-key反例。

同上六组pytest最终`164 passed in 0.42s`，ruff通过；schema字段未因此再改。最终代码已交fresh reviewer复验：

- holmes_bridge.py: `765ecd5cc06a9aa08e2f31a179d66f5b3ac14c59bdc14f8f673d6e7a93b6c5b4`
- outcomes_v4.py: `880b924b9ebaaa03a1ee032e2e3753ef2ae312d27afd59b800ae47ceb6c2fb86`
- tests/test_m0_holmes_bridge_v4.py: `59c150296b3b9b3bdf6a34e2cc50bf885dfc7b7518d09994bb71fbb137cbaa53`
- tests/test_m0_outcomes_v4.py: `c419114128248d9834a861356887ec9431cbc06fd625e13cdc06174b9d7d67a2`

store、v3、schema的前述hash不变。协议规则复用只作用当前strict v4，显式旧v3仍保持原解释。
