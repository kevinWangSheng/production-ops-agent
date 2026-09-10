# PR16 严格 v4 合同修复与实现者证据

基线`da02563`。对应comments `3976067577`（完整报告丢失）、`3976067579`（可选target绕过）、`3976067585`（缺适用时效），统一包含独立审查发现的反证/排除、末次人控状态、空报告时钟和范围旁路。依据[已审设计](round-02-pr16-strict-seam-design.md)，本轮仅离线实现。0新增模型/trace/环境/PG操作；旧schema、旧源码快照、真实报告/账本、质量FAIL保持。

## 原缺陷证据

主要红证据为旧最高seam的实际错误接受与完整报告丢失，不把“新模块尚不存在”的初始化错误当缺陷证明：

- 实际normal03原报告summary 1218字符、next_steps 4项均未进入Outcome；16/16 claims target=None。只读查询输出上述计数，没有读取provider私有内容。
- 原入口6例均返回无违例：无scope的实例fact；failed+错target的counter_evidence/rejected_hypothesis；未来capture；历史窗数据却用当前措辞（入口无结构化用途）；最后cancel但completed。
- 开发中独立审查进一步复现完成空/假设报告可省略全部输出时钟、假设提供无效target/time refs未拒、current新鲜判据可绕过query window；均纳入同组。

自然语言“currently”等是否与声明用途矛盾，仍由完整报告独立质量审查负责；本实现不声称机器已理解所有文本含义。

## 当前版本与完整性

新增`outcomes_v4.py`、`m0-public-v4`与`m0-report-v2`机器schema。公共身份/投影/结构检查复用v3，不复制另一套运行平台。当前`holmes_bridge.load_packet`和CLI默认严格v4；历史回放必须明确调用`load_legacy_packet`或`--legacy-v3`。

新Outcome携带完整report解析对象、原始安全content及hash，绑定同Run/step/request/generation的trusted报告捕获；summary、next_steps、claims、gaps不再白名单丢失。原文、hash、解析对象、实际response/result/capture逐项比对。任何存在的模型报告均需可信有序输出时钟；完成声明仍需唯一committed交付。无模型的真实incomplete/handoff出口保留。

fact/counter_evidence/rejected_hypothesis统一要求模型明确target_refs/time_scope_ref；每target须被该claim引用的实际交付view支持。授权catalog存在不等于已观察；Integration不提升为实例。非事实类型可无引用，但提供的evidence/target/time引用必须解析。

TemporalPolicy明确适用integration/interface和target refs或当前授权scope revision，不默认覆盖所有对象或编造TTL。源时间、采集完成、dispatch→response区间与evaluation分开。current按整个可见事件范围保守判断，不以最新一条掩盖旧事件；historical按可信可见事件范围和历史窗判断，不因晚些重放而stale。源sample未知/阈值缺失为unknown；Prom求值时刻不被当sample age。源事件不能越实际query window，spill保留原raw但不作合格事实。末次cancel/correct对齐cancelled/waiting_human；new_run后才可新执行，旧控制不永久阻断。

## runtime共享接口

`ModelReportV2 / EvidenceContext / TimePolicy / Timing`由同模块提供。`build_context(..., allowed_scope=scope)`只从准备实际交付且已登记hash的view构造catalog；按当前授权scope验证服务，失败/拒绝视图不开放target refs，不导出整个registry。`validate_report_context`返回结构scope/status引用错误；完整时间和输出绑定由`check_outcome`负责。

runtime另一执行者实现实际payload中catalog/hash/原字节预算、独立evidence-timings/time-policies/report-capture、客户端时钟与可见event-time；其固定Holmes假pipe证据另有记录，不能视为新版真实模型运行。原observed_at不回填为collection完成，旧initial raw证明不足仍不能在严格桥接中补造。

## 离线验证和历史兼容

```sh
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes.py -q
```

输出`142 passed in 0.22s`，其中v4 checker14项、strict bridge4项；Ruff和diff检查通过。覆盖完整报告篡改/丢失、三类事实scope/status/time、历史/current/未来/未知、源窗spill、无claims时钟、初始与动态context、末次人控和legacy版本边界。

实际旧fault01/normal03都用原22a96源码回放：默认v4返回LEGACY_REPORT_REQUIRES_EXPLICIT_REPLAY及strict_scope/freshness=unknown；显式legacy仍可得到原结构结果，但current_acceptance_pass=false。不补目标、时刻或v2字段，不将19/19、12/12或原质量FAIL变成新协议通过。

完整安全原报告的新增回放由父保存在ignored本地`tmp/m002-v4-replays/`，tracked同名JSON仅作metadata/hash/数量/violations/本地路径摘要；遵守原始业务JSON不Git导出的边界。CLI/Outcome完整报告行为保留，独立审查可读取本地全文和原始业务报告比对。本文不覆盖父维护的public摘要。

当前仍等待fresh-context整组独立终验/最新CI与审查闭环；不打开M1、重跑已用尽预算或声称新版真实模型已验证。

稳定文件SHA-256：
- `scripts/m0/outcomes_v4.py` `ab47bf8c4e5f968771ff9980c200f9be54c68b61f528abccd54e2bee3b42896d`
- `scripts/m0/holmes_bridge.py` `bc257b2b6f0095b72148d1f790aefc352766b0f0d999701c3a65a898b9196f17`
- `tests/test_m0_outcomes_v4.py` `4a05fc8ac81981b2eb3fff06a2a5cc7283b708b8452793ce59fc31ad0f21b2fa`
- `tests/test_m0_holmes_bridge_v4.py` `8d950c07adc185f849d2e9467fbdaee21e66cd4c24f3342e650285cac4a8416a`
- `tests/test_m0_holmes_bridge.py` `2592807a778d0a3e23899b220f221a4cda9a63b4cd779a44952dcfe3a859acc1`
- `docs/evidence/m0-real-investigation/IncidentScenario.v4.schema.json` `2cbeb855298c9c29a4e3d6012b6dd73ee6affd1524382fb56fb20cb1e6e7046d`
- `docs/evidence/m0-real-investigation/IncidentOutcome.v4.schema.json` `e019fee6a355f2c586389c991a2887bf37bfc1caec9fcc538eed852bf014b53a`
- `docs/evidence/m0-real-investigation/ModelReport.v2.schema.json` `11508bf884c65c045aa0963bf5305ba2a943bda3e1aef821b7e2bef06e398d4b`
