# PR16 P1：prepared 请求的响应采纳边界

基线`aa485f9`，CodeReview comment `3976615675`。本修复仅机制与相关测试；不改v4、schema、预算平台、旧记录或历史运行证据。0模型/trace调用，没有读取旧private记录或.env，没有起停PG、提交或推送。

## 复现与相邻窗口

实现者和新上下文独立审查均确认：prepare_request已经持久化dispatch但send_grant.claimed=false，旧commit_response仍接受任意合格assistant，随后可以publish。另有同因两事务窗口：旧prepare_request先提交dispatch/预算、再插grant；若后一步失败，会留下无grant的半条记录，被误判为兼容direct路径。

先写真实PG替身回归后运行：

```sh
M0_STEP_POSTGRES=1 /Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/integration/test_m0_step_store_postgres.py -k 'prepared_unclaimed or failed_prepare_grant' -q
```

红结果`2 failed, 18 deselected in 0.70s`：第一例未抛应有拒绝；第二例grant插入故障后得到(dispatch=1, grant=0, reserved=10)，而应整笔回滚。**10为随机隔离替身账本单位，不是实际CNY或真实调用。**

## 最小修复

- prepared路径在同一业务事务创建ModelStep/预算/dispatch/执行身份及unclaimed grant；不再有后续独立插grant事务，也不会调用starter。失败前未提交的数据整体回滚。
- commit_response在原current Run/owner/epoch/control generation/有效lease/最新request检查之后，再拒绝存在但未claimed的grant，固定错误`MODEL_REQUEST_NOT_INITIATED`。
- 无grant的直接dispatch保持原有trusted starter合同。工具没有独立prepare路径，仍由原dispatcher/attempt/fence条件提交。没有新增表或第二套账本。
- publish继续要求已提交JSON候选；没有response时明确`UNCOMMITTED_CANDIDATE`，避免因response=None抛AttributeError。新拒绝路径不能留下可发布响应。

claimed仅表示一次性发送许可已被受控transport消费，不独立证明provider收到内容或外部exactly-once；实际传输证据仍归可信client。此规则不用于倒推、重签或改写旧运行的actualsend证据。

## 通过证据

上述两个红用例修复后`2 passed, 18 deselected in 0.62s`。另覆盖：claimed prepare/direct无grant均可正常提交发布；claimed不绕过独立Run/owner/epoch/generation/lease及最新attempt；真实spawn进程prepare提交后`os._exit(23)`，留下完整dispatch+unclaimed grant，父进程拿旧fence尝试伪提交仍拒绝且预留保留。grant插入失败只作用于新随机事务，不改旧数据。

```sh
M0_STEP_POSTGRES=1 /Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/integration/test_m0_step_store_postgres.py -q
```

输出`24 passed in 7.72s`，Ruff `All checks passed!`，`git diff --check`退出0。PG由root启动并验证原身份，本执行者不负责生命周期。独立终验记录由另一个Agent维护；本记录不代替独立复验或最新CI/Security闭环，不开放M1。

稳定SHA-256：
- `scripts/m0/step_store.py` `c9a1e82c1945152a94d28202fd0f1a91007d44b99fc813bef5d0870af2ac349e`
- `tests/integration/test_m0_step_store_postgres.py` `421df91659793228d2b18641af1f1d22d9b46ecf0c69d45935919ca418b7e9a7`

## 适配器版本边界

新的prepared/grant原子语义使用`pg-private-pipe-v3-atomic-send-grant`，不继续沿用旧`pg-private-pipe-v2-content-normalization`。已静态核对first阶段accept/claim与second阶段claim均使用同一VERSIONS；second还先检查完整code_hashes/profile。StepStore在可领取旧Run上发现versions不兼容时按现有路径blocked(INCOMPATIBLE_STATE)，不静默升级；driver更早的hash拒绝也不会改写旧记录。

code_hashes/profile_hash不匹配即拒绝的防线在本补丁之前已经存在；本次只同步原子send-grant的语义版本标识，不表示此前完全没有跨版本门槛。

旧真实两请求结果保留为其旧版本只读历史，不因为新规则而获得新的actualsend证明。未完成旧版本续接必须沿既有handoff/显式新Run路径，仅继承允许业务事实；不搬迁provider私有状态、不迁移/清洗旧行或重置预算。本变更未运行PG或模型。

版本补丁离线验证：`python -m pytest tests/test_m0_pg_live_probe.py -q`（主既有.venv），10项通过；Ruff及diff检查通过。

新增稳定SHA：`scripts/m0_pg_live_probe.py` `1ccf6255f51f65752c86d3a7ae7eb80177b4bcdf4fe4498ed6daaf543c2fc60f`。
