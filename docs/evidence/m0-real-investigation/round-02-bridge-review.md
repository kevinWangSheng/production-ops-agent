# M0-02 Holmes→v3 实际工件独立核验

日期2026-09-10。状态：**parser/source修复后真实normal02可完整装载；合同判据仍有FAILED_EVIDENCE_AS_FACT，未整体通过**。0模型/trace/环境操作，未读.env/private。

当前14项bridge+v3定向tests通过（0.10s）；动作记录已改attempted/authorized/executed三态，HTTP响应仅证明代理请求发生，未知backend执行不伪报未执行。事实实例身份只有显式claim.target时才确定性检查，未标target的自然语言身份/因果仍需独立质量评审。

实际运行：`.venv/bin/python -m scripts.m0.holmes_bridge --run-dir /Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmes-runs/m002-normal-02 --projection-source-sha256 7fd7326644b26fe00d0a8bad25aab4b865dfbd453bb5a7b4fa491eda5539a676` 返回contract_consistent=false,INVALID_ARTIFACT。

定位load_packet→extract_registered_views的decoder.raw_decode(rest)：当前假定tool_call_metadata后立即为JSON。真实Holmes e10/e11/e13失败工具在metadata后有固定Tool execution failed前缀及转义换行。应按固定上游源码允许精确失败prefix，再校验registered errorview和tool/call身份；禁止任意扫描寻找JSON（避免正文伪造evidence_id）。原JSON结果、view/raw/ledger不改，修复无需重发模型。

在失败点之前，实际15项raw/view精确文件hash、canonical hash、纯投影重放、registry/subject均已核过。该事实不等于完整桥接通过，更不等于normal02报告质量通过。修复后须对真实Run重跑并补hash。

## parser与冻结SOURCE修复复验

错误prefix已按固定上游格式精确解析，并要求对应失败view；显式projection-source-file和SHA强制匹配，可使用原7fd732冻结完整snapshot，不用当前新算法重签旧Run。独立17项bridge+v3测试PASS/0.11s；实际CLI增加--projection-source-file指向环境docs/evidence/m0-real-environment/immutable/7fd732...py.txt后，成功装载15raw、15delivered views、最终请求m002-normal-02:http:11、initial_views0。全部文件hash及旧投影重放通过。

check_outcome仍返回FAILED_EVIDENCE_AS_FACT：当前fact统一要求status ok，因而“3个工具查询被拒/证据被隐藏”的执行事实也被拦。该限制不是parser失败，不应简单放开所有failed证据；可后续显式区分tool_execution与investigated_system事实scope。当前保持失败判据和原报告，不为变绿松规则。normal02独立语义质量不通过亦仍保留，两问题不能互相替代。

本复验未新请求模型、未读private、未改真实工件。当前review只认证parser/source/交付完整性修复，不宣称全部合同或报告质量通过。

最新覆盖hash：
- `scripts/m0/holmes_bridge.py` `d6dc19f7baf90d1dc6a24a6e1b1364d515631f75c0dc5452b1a6dd4d9860ccf6`
- `scripts/m0/outcomes_v3.py` `6f37be4fedd404164ea4b88daedb1b956b0462e034d68a3f7e8bf16687df4a92`
- `tests/test_m0_holmes_bridge.py` `c413ef0e5d4096c7ff204ef020e8cf602f3df4a1cc253fae7f20d974035e84c7`
