# F11 U2 校准证据合同（预审原文，裁决列见任务记录 r1）

- 预审：Codex（gpt-6-astra，high，只读），2026-10-14，基于 `origin/main` `50cc62a`；未访问 kind、未读凭据。
- 裁决：用户 2026-10-14 对 U2-01–U2-04 全部选推荐；L1–L4 为 lead 可逆默认。
- 文中链接为预审时的本机绝对路径，对应仓库相对路径。

**已确定**

本次基于 `50cc62a` 只读核查；未访问 kind、未读取凭据、未修改文件。以下数值建议均为待验证方案，不是校准结论。

- **对象应选 `otel-demo/Deployment checkout`。** 当前 profile 经实际加载计算为 `otel-demo-checkout@b72bbe2e30be`；评估窗／freshness／单查询超时为 **300／90／20 秒**，cadence／健康窗／deadline／次数为 **60／600／3600 秒／40 次**。原校准流量约每五分钟四单，500ms 延迟上限明确不是回归检测阈值。[profile:11](opspilot/observer/profiles/otel-demo-checkout.json:11)、[原证据:28](docs/evidence/m1-02-health-profile/run.md:28)
- **五项身份必须来自当前注册表并与数据库一致**：`integration_id`、`cluster_uid`、`namespace`、`resource_uid`、`workload`。执行时核对 integration 标签、`kube-system` UID、实际 namespace/Deployment；`resource_uid` 取注册表键，不能擅自换成 Deployment UID，后者另存辅助证据。历史记录曾使用不同 cluster 标识，当前值尚未确认。[schema.py:185](opspilot/schema.py:185)、[release_commands.py:608](opspilot/observation/release_commands.py:608)、[历史核对:14](docs/evidence/m1-184-self-heal-live/run.md:14)
- **正式清单当前为空，1200 秒只是候选。** 授权须匹配五项身份、profile revision／完整哈希、`min_tracking_seconds` 及 profile 冻结参数；不能修改旧 pending 的参数或借重放重新计时。测试清单不能进入产品清单，也不能称为正式校准。[PR-1:59](docs/tasks/2026-10-12-f11-pr1-contract.md:59)、[PR-2:109](docs/tasks/2026-10-13-f11-pr2-contract.md:109)、[PR-2:379](docs/tasks/2026-10-13-f11-pr2-contract.md:379)、[P2-04:878](docs/tasks/2026-10-13-f11-pr2-contract.md:878)
- **版本证据靠 Pod UID→ReplicaSet→完整容器 digest 集合**，不能靠固定的 `service_version`。仅配置发布允许镜像相同，但 before/after template hash 必须不同。after degraded 才判异常；仅 context degraded 判 unknown。r1–r7 的计次、缺口和部分证据规则继续适用。[探测:7](docs/evidence/f11-pr2-telemetry-probe/run.md:7)、[PR-2:488](docs/tasks/2026-10-13-f11-pr2-contract.md:488)、[release_evaluation.py:698](opspilot/observer/release_evaluation.py:698)

**决定表：需要用户裁决**

| 编号 | 问题 | 证据 path:line | 选项 | 推荐 | 可逆／合同层 |
|---|---|---|---|---|---|
| U2-01 | 首次 policy 要证明多长的延迟回归覆盖？ | [C3:372](docs/design/technical-proposal-2026-09-07.md:372)、[PR-2:925](docs/tasks/2026-10-13-f11-pr2-contract.md:925) | A：发布后 0、5、15 分钟起病；B：扩至 30 分钟，可能需要新 profile／更大预算。 | **A**；另测临近预计 healthy 终结的边界。明确覆盖范围，不宣称监测满一小时。 | 合同：覆盖承诺 |
| U2-02 | 外部故障能否作为 U2 的回归刺激？ | [kind_lab.py:21](scripts/kind_lab.py:21)、[PR-2:879](docs/tasks/2026-10-13-f11-pr2-contract.md:879) | A：checkout 真实配置滚动发布后，定时注入 paymentFailure；B：制作带即时／延迟故障开关的 checkout 测试镜像，成本更高。 | **A，仅证明发布后异常检测与时间覆盖**；不能证明发布导致故障。若要求版本内生回归，则选 B；现有工具不足。payment 故障不能充当“无关故障”。 | 合同：证据可支持的结论 |
| U2-03 | 样本量、成功与失败标准 | [PR-2:925](docs/tasks/2026-10-13-f11-pr2-contract.md:925)、[feature_list.json:114](feature_list.json:114) | A：开发探测后，每类独立验证 3 次；B：每类 10 次，增加环境占用。 | **A，共五类×3次**：健康、即时、延迟5分、延迟15分、健康发布并发无关故障。健康零异常误报且能终结；回归全部在终态前且 deadline 内检出。预热后非预期 unknown ≤5%，全程比例另报；健康终结剩余≥5次、≥300秒。小样本不构成可靠性统计保证。 | 合同：验收口径 |
| U2-04 | 如何采证并冻结正式参数？ | [PR-2:113](docs/tasks/2026-10-13-f11-pr2-contract.md:113)、[PR-2:381](docs/tasks/2026-10-13-f11-pr2-contract.md:381) | A：旁路真实采集＋离线判定，独立核对真值；B：临时测试清单驱动 Observer 作为主要证据。 | **A 为校准主证据，B 只作接线验证**。先沿用现有 profile；M 从实测选取。任何阈值／时间／预算变化生成新 profile revision，重新验证后提交用户批准。 | 合同：证据资格及正式冻结 |

失败处理适用于全部验证：保留失败轮，区分注入失败、环境干扰和产品漏检；**不删失败、不事后放宽判据**。修改参数后重新冻结候选并重跑受影响验证集；证据不足则清单继续为空。

**决定表：lead 可定的可逆实验细节**

| 编号 | 问题 | 证据 path:line | 选项 | 推荐 | 可逆／合同层 |
|---|---|---|---|---|---|
| L1 | 发布方式与无关故障 | [C3:420](docs/design/technical-proposal-2026-09-07.md:420)、[values.yaml:7](scripts/kind_lab/values.yaml:7) | 切换固定 digest；或改变 Pod 模板环境变量。 | 首轮选**无业务作用的实验标识环境变量**触发 checkout 滚动更新，保存完整前后模板及 digest。无关故障候选为 `ad` 的可恢复故障；必须先证明 checkout 必要信号及流量未受影响，否则换对象／判该轮不满足场景。不运行 chart 重建。 | 可逆；若改变覆盖声明，回到 U2-02 |
| L2 | 采集路径 | [release_sampling.py:70](opspilot/observer/release_sampling.py:70)、[release_evaluation.py:166](opspilot/observer/release_evaluation.py:166) | 自写 PromQL；或复用公开计划。 | 复用 `build_release_query_plan`：同一 T，串行 mapping/context→UID集合→after，保留原始响应；离线 `evaluate_release_sample`＋窗口推进。独立审查者按注入日志、实际请求结果和 Kubernetes 对象核对，不能只以 evaluator 自洽为通过。 | 可逆 |
| L3 | 现有工具缺什么 | [kind_lab.py:987](scripts/kind_lab.py:987)、[kind_lab.py:895](scripts/kind_lab.py:895) | 手工拼操作；或有界工程 harness。 | 缺：发布前后快照／事件生成、时间线 watch、定时注入、无关故障及恢复钩子、发布查询采集与离线折叠、环境占用锁。现有仅有环境准备、健康检查、账号和 paymentFailure 注入／恢复。**本次只列缺口。** | 可逆 |
| L4 | 占用与凭据 | [kind_lab.py:112](scripts/kind_lab.py:112)、[Observer入口:14](opspilot/observer/__main__.py:14) | 共用运行环境；或预约串行批次。 | lead 与其他会话预约，记录持有人／期限／基线；一次一个故障，恢复且健康窗洗净后再下一轮。按每批3轮预留 **3–4小时**，另预留基线测量30分钟，均为估算。旁路 source `prometheus-lab.env`；Observer source 自己账号，独立 DSN；禁打印环境、凭据或认证头。 | 可逆；不得覆盖其他会话修改 |

**测量与参数推导**

1. **时间与身份**：记录部署源成功提交的 `released_at=R`、请求／回执时间界限、新 Pod Ready、旧 Pod 消失、RS／Pod UID／容器 digest、首次映射充分及首次切片充分时间。记录配置传播、实际故障生效时间，区分“计划 R+N”与“实际起病”。统一 UTC、测源／DB 时钟偏差；未来时间拒绝仍为零容差，不能以 Ready 时间替代 R。[PR-1:60](docs/tasks/2026-10-12-f11-pr1-contract.md:60)

2. **遥测与成本**：用原始样本时间戳测 scrape 周期、可见延迟；以实际请求／span 时间对齐 spanmetrics 首次可查时间，测 flush 加传输延迟，无法拆分则报告合并值。逐窗保存新旧 UID 流量、覆盖点数、新鲜度、每查询及整轮耗时、unknown 原因、覆盖缺口。当前完整计划最多 **49个串行查询**；须复现120秒租约、5秒提交余量和单响应大小保护，不把诊断额外查询计入产品覆盖。[release_evaluation.py:230](opspilot/observer/release_evaluation.py:230)、[store.py:70](opspilot/observation/store.py:70)、[sampler.py:97](opspilot/observer/sampler.py:97)

3. **按真实调度折叠**：A 取合法发布事件已齐备、实际可授权的时刻，不倒填 R；首采 A+I，后续为提交时间+I，再加轮询／排队。设首个连续充分健康窗口末尾为 T₀，无中断时 healthy 至少满足  
   `T ≥ max(R+M, T₀+M−W, T₀+H)`，且提交 `< R+deadline`。  
   用实测起病至检测延迟，加安全余量，确保最晚目标回归在可能的健康终结前可见；再逐轮验证40次预算。unknown／缺口必须按合同重置；不能用平均耗时或 `40×60` 代替验证。[PR-2:448](docs/tasks/2026-10-13-f11-pr2-contract.md:448)

**证据与批准**

`docs/evidence/f11-u2-calibration/` 保存 `run.md`、冻结 `summary.json`、逐轮摘要及原始响应哈希清单；摘要包含代码／profile／目标身份、场景真值、时间线、查询计划、全部成功与失败、覆盖／预算统计、原始包哈希和平台链接。原始 ledger 不入库；按 ADR-0006 上传实验平台供独立审查，凭据不进入任何证据。[ADR-0006:16](docs/adr/0006-trace-evidence-and-backlog.md:16)

policy 的 `evidence_path` 指向该冻结 `summary.json`，`evidence_sha256` 哈希其**确切文件字节**；摘要绑定原始响应清单，避免摘要与 policy 相互哈希。用户在可引用评论／决定记录中批准**摘要哈希、目标、profile完整哈希、M及覆盖限制**，据此填写 `approval_ref/approved_at`，再计算规范 content 哈希和 revision。[release_policy.py:97](opspilot/observation/release_policy.py:97)

写入正式清单会首次开放授权，属于**用户门 PR**，不能按纯证据／chore 自动合并；独立审查与最终 CI 完成后交用户合并。U2 不覆盖 F11 的全部调查与 old/new 对比验收，`passes` 保持 false。[AGENTS.md:67](AGENTS.md:67)、[feature_list.json:114](feature_list.json:114)