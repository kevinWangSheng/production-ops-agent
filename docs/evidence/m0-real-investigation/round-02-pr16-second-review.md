# PR16 第二轮P1独立复验

日期2026-09-10。状态：**两项P1修复后本地独立复验通过，无新增阻断发现**。审查者不修改实现/测试，不提交推送，无模型/trace/.env/private读取，无PG起停。

原发现已通过GitHub只读API核对：3975937306、3975937313，均审查1462e2da7b557d59e4cbec903dc69ddd6f3d59dc。另完整读取冻结docs/testing/first-investigation-v3-2026-09-10.md：报告须绑定实际request，未完成handoff不能伪装报告完成；4模型/20工具为per-Run子限额，所属实验累计预算/次数/unknown/绝对deadline不得重置。

## 独立失败复现

1. 由现有v3有效packet构造completed/inconclusive、claims/evidence_ids空、trusted.deliveries空，当前check_outcome返回[]。这是错误接受，不能以没有引用跳过完成证明。需要独立于view遍历要求恰一条匹配Run/step/request/current generation的response_committed交付；合法空证据completed不确定应能带此完成记录通过。incomplete人控handoff不要求伪造模型响应。
2. 实际PG随机实验9fee4c08-726e-4b30-a839-e2de87830712，第一Run发起一次模型替身及一次工具替身，然后new_run；新Run子限max_requests=1错误被REQUEST_LIMIT拒绝。用较宽模型子限建立第二Run工具计划后，max_queries=1仍错误被QUERY_LIMIT拒绝。两次拒绝对应experiment全历史计数，而不是本Run。实验reserved累计20/limit100，历史未删改。

上述测试仅本地替身starter，无真实HTTP；随机实验行保留，不修改旧表。作者收到具体复现与匹配唯一性/全轮预算保持要求，后续冻结再复验。

## 修复后独立复验

作者稳定后读取原红绿记录round-02-pr16-report-binding-run-limit-fixes.md并重读实际diff。matching_reports在view循环外核对current Run、step、physical request、最终generation及response_committed；execution或assessment任一completed须恰1条。空views合法完成、真正incomplete/handoff可0条；旧Run/旧代次和重复记录仍拒。子额度计数绑定不可变ModelStep.run_id而非可变subject.current_run；历史记录、PG累计预算与共享全轮guard均未重置。

独立命令：

```sh
M0_STEP_POSTGRES=1 .venv/bin/python -m pytest tests/test_m0_outcomes_v3.py tests/test_m0_outcomes.py tests/integration/test_m0_step_store_postgres.py tests/test_m0_pg_live_probe.py tests/test_m0_holmes_bridge.py -q
```

**152 passed in 8.86s**。覆盖新两P1、上一轮new_run/current-control跨层、v2回归、真实PG同Run跨epoch额度不重置、新Run4/20子额度可用、unknown/累计费用/原绝对deadline保留，以及临时共享账本跨5Run20请求后重载第21拒绝。模型和工具均替身，临时全轮账本不接触真实费用总账。

另独立重复原失败probe：completed无delivery现在REPORT_DELIVERY_MISMATCH；blocked/incomplete/handoff无delivery仍[]。新随机PG实验a9d894f4-c555-4dab-b186-377e13b08d02两个Run各max_model=1/max_tool=1均实际允许本地starter，累计reserved20替身单位仍保留（非20CNY、无真实HTTP）。

## 原真实工件回放

直接只读运行holmes_bridge CLI，指定原22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8完整源码及SHA，不使用新投影重签旧Run：

- m002-fault-01：contract_consistent=true，无violations，19captured/19delivered，report_request_id=m002-fault-01:http:17，initial_views0。
- m002-normal-03：contract_consistent=true，无violations，12captured/12delivered，report_request_id=m002-normal-03:http:20，initial_views0。

新增唯一committed要求未破坏实际原工件结构重放；两报告的独立语义质量FAIL不变。未覆盖旧结果文件、未读private、未发新模型/trace。

## 稳定版本和交接

作者5文件hash与独立检查全部匹配；v2、原fixture、PG预算实现与共享round02 guard均无本批diff。
- `scripts/m0/outcomes_v3.py` `ab5fbe76ff01575c5ec2250417c38c29c4e577461eb9909c8312baf7cbaf3a8d`
- `scripts/m0/step_store.py` `3e0ea6a3f56f63e982b608e52c73007089c42e4ebafdd9c2e5a3aa9b41e6e255`
- `tests/test_m0_outcomes_v3.py` `b12708112a3bf600a941044f44765dd44e5c841d475cfcd05743f7a150ca69ed`
- `tests/integration/test_m0_step_store_postgres.py` `04f8e9bff8fee23ca8809961f0705615a74e486a00857f4817f984e7fdae2271`
- `tests/test_m0_pg_live_probe.py` `89ff36628074fa0d920c4a8aa65f8c5910489c24ab74cdab1088a1910ebf7de8`

本审查只新增本记录，未改实现/测试/用户状态文件，未提交推送。PG检查结束，由父执行者停止专属PG。此本地通过不代替最新CI/Code/Security复审闭环，不授权合并或打开M1。
