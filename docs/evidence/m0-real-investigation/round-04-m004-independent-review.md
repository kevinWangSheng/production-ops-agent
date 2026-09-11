# M004 独立只读审查（ea877c8）

审查范围：仅检查提交 ea877c8 中两份 business-summary、两份 observation JSON、round-04 质量记录及其声明；未读取凭据、private protocol、reasoning、注入答案；未运行模型/trace/服务。

## 确定性核对

- 两个 summary 的 `run_id`、`report_schema_version=m0-report-v2`、`assessment_status=completed`、`conclusion=partial` 与 round-04 记录一致。
- normal summary 明确把 checkout 缺少 ERROR series、有限 trace/log 展示、缺少 SLO/baseline、未知 source time 等列为 gap；没有把空结果写成零，也没有认证 healthy。
- fault summary 明确记录 frontend-proxy `/api/checkout` 500、checkout PlaceOrder 与 payment Charge 错误、共享 trace id，并明确 parent edge、token origin、日志、计数分解和 SLO 等 unknown；没有把共享 trace id升级为直接 parent-child 证明。
- observation 中的 raw_path/hash 结构与报告 evidence 引用可对齐；但 ea877c8 的 tree 不包含这些 `tmp/m0-environment/raw-evidence/m004-*` raw 文件，故本审查无法在提交内重算 SHA256 或逐条回读 view/raw。此为证据可复核性限制，不能据摘要自证 hash 正确。
- 未发现将用户接受的五条 unknown 改写为成功、将采样/缺失视为零、或把调查 partial 写成总体健康/恢复认证的行为。

## 裁定

### m004-normal-01

- **P1：无。** 摘要的核心负向结论（当前所见没有 checkout failure）与 unknown 边界写法一致，未见安全/权限越界或虚构健康认证。
- **P2：有，证据可复核性/样本资格阻塞。** observation 只提交了 raw_path 与 hash 指针，raw/view 原件不在 ea877c8 tree，无法独立重算 hash；且视图本身是有界采样。该 Run 可作为“normal 方向候选/披露 partial 结果”，但在 raw 工件可取得并通过哈希重算前，不能计入 v4 normal 正向样本。
- **P3：无新增。** 已披露的 coverage、日志源、查询 allow-list、HealthProfile/SLO 五条 unknown 均已正确保留。

**是否计入 normal 正向样本：否（当前提交证据闭环不足；不是报告把 unknown 写错）。**

### m004-fault-01

- **P1：无。** 500、checkout/payment error 与共享 trace id 的故障方向有多视图交叉支持；未越权执行修复或认证恢复。
- **P2：有，证据可复核性/样本资格阻塞。** 与 normal 相同，提交缺少 observation 所引用的 raw evidence 文件，无法独立重算 raw hash、复核 view 细节；另外 parent-child edge、token origin、日志覆盖和总 500 数仍是 summary 已披露 unknown。故障结论可作为方向性 partial 候选，不能在本审查中闭环为 v4 正向样本。
- **P3：无新增。** summary 对共享 trace-id 仅作 well-grounded inference、未宣称直接 parent edge，符合接受的 unknown 边界。

**是否计入 fault 正向样本：否（当前提交证据闭环不足；故障方向本身未发现 P1/P2 语义错误）。**

## 结论

两 Run 的报告 claims/gaps 与 v4 质量约束及用户接受的五条 unknown 一致；本次独立审查没有发现 P1，也没有发现因 claims 误述造成的 P2。唯一 P2 是提交物未包含 observation 所引用的 raw/view 原件，导致 hash 与逐条证据无法独立复核；因此两者均暂不能计入 normal/fault 正向样本。需补齐与 hash 对应的 raw/view 工件后重新进行确定性复核。

## 复审（HEAD 4925107，raw evidence 已提交）

重新按 observation 的 `raw_path` 映射到 `docs/evidence/m0-real-environment/raw-evidence/`，对 normal/fault 各 7 个 raw 文件使用 Git blob 内容计算 SHA256，并与 observation 的 `raw_sha256` 逐项比较：**normal 7/7 匹配，fault 7/7 匹配，无差异**。路径均存在且可从提交读取；未读取凭据、private protocol、reasoning、注入答案，未运行模型/trace/服务。

此前因 raw 缺失产生的证据可复核性 P2 已关闭。重新核对 summaries 与 observations 后：

- **m004-normal-01：P1 无，P2 无，P3 无新增；可计入 normal 正向样本（1/2）。** 该 Run 仍是 partial 观察，不能认证总体 healthy；coverage、日志源、查询限制、HealthProfile/SLO 等已接受 unknown 继续保留，属于披露限制而非质量缺陷。
- **m004-fault-01：P1 无，P2 无，P3 无新增；可计入 fault 正向样本（2/2，如此前 m003d-fault-02 已计入 1/2 则 M004 后为 2/2）。** 故障方向证据与 raw hash 完整闭环；parent-child edge、token origin、日志覆盖、总 500 数及 SLO 等仍正确保持 unknown，不影响“可诊断故障正向样本”资格，也不构成总体健康/恢复认证。

### 复审结论

HEAD 4925107 已补齐并提交两 Run 全部 raw evidence，hash/path 可确定性复核通过。两 Run 均满足本次 v4 质量样本的独立证据闭环：normal 可计入 normal 正向样本，fault 可计入 fault 正向样本；未发现 P1/P2。原报告中的 partial 与五条已接受 unknown 不应改写。
