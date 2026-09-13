# B4 真实恢复验证结果

执行日期：2026-09-12。依据已批准的 [`round-05-recovery-contract.md`](round-05-recovery-contract.md)。本轮使用新 allocation `m0-05-b4-20260912`，不改历史账本；专属 PG 与 `m0-otel` 已在本结果完成后停止。

B4-1 启动时使用的短运行截止在等待人工/环境步骤期间过期；B4-2/B4-3 改用同一已批准 allocation 的新截止记录（[`round-05-b4-deadline-2.txt`](round-05-b4-deadline-2.txt)），没有重用旧账本、增加 HTTP 或扩大费用上界。该时间处置保留在原始输出中。

## 结果矩阵

| 项目 | 结果 | 实际证据 |
|---|---|---|
| B4-1 首流程逐步骤恢复 | **通过（机制层）** | 首阶段真实模型 HTTP 200 后进程 exit 17；第二进程真实 PG 重建、保留 evidence/message pairing 并完成第二次 HTTP 200 发布。见 `round-05-b4-1-first.txt`、`round-05-b4-1-second.txt`；Run `b960bf7e-05e9-44b1-bf0f-3454ddf65681`。 |
| B4-2 取消后迟到结果 | **通过（机制层）** | 真实模型 HTTP 200 已发出后提交 cancel；迟到 response `late_response_accepted=false`，PG 状态 `cancelled`/generation 1。见 `round-05-b4-2-cancel.txt`；Run `acba5e7a-db55-43a5-a83d-b5aac46e6413`。 |
| B4-3 版本不兼容 handoff | **通过（机制层）** | 已提交未完成 step 在不兼容 tool 版本 claim 时返回 `INCOMPATIBLE_STATE`，PG 状态 `blocked`，audit 记录 accepted=false；无模型 HTTP。见 `round-05-b4-3-incompatible.txt`；Run `be1a0c92-9572-4bf0-a501-535d88ab94ec`。 |

三项每个均执行一次；B4-3 第一次因 `LEASE_ACTIVE` 失败，原始输出已单独保存在 [`round-05-b4-3-incompatible-first-failure.txt`](round-05-b4-3-incompatible-first-failure.txt)，后续有效重跑未以失败样本补分。该结果证明有限 PG/协议机制，不证明完整产品调查质量、生产恢复或 M1 入口。

## LangSmith 白名单出口（B6 子项）

三次 Run 均完成安全 DTO POST/同 Run GET 回读：

- `b960bf7e-05e9-44b1-bf0f-3454ddf65681`：[`round-05-b4-1-trace.json`](round-05-b4-1-trace.json)
- `acba5e7a-db55-43a5-a83d-b5aac46e6413`：[`round-05-b4-2-trace.json`](round-05-b4-2-trace.json)
- `be1a0c92-9572-4bf0-a501-535d88ab94ec`：[`round-05-b4-3-trace.json`](round-05-b4-3-trace.json)

每份返回 `TRACE_VERIFIED`，仅包含 run/session/name/type、受限 inputs/outputs；`private_fields_persisted=false`，没有 provider reasoning、prompt 凭据或原始私有业务字段。404 采用有界回读重试；409 视为同一 run 的幂等 POST，不重复模型调用。

## 工件 SHA-256

| 工件 | SHA-256 |
|---|---|
| B4-1 first | `6f1f16b61c1be8d95902b396ccfec8963c405d7835576c3fb03672efa277f6a7` |
| B4-1 second | `081356f54d0bc81262e1c723649a6402d8e2cb1a14eebdf64c17e553453c03ad` |
| B4-1 trace | `1780489375caddcd6ec0101b0f58d5d44cc59073c46319fd3e9dcb03cd2cd9d8` |
| B4-2 cancel | `a0785b36348e2426190ba47505cc969dac0ef2d300d80a596adac95110132d4b` |
| B4-2 trace | `c29b435392a4adca5f5373c9695e7db4c0e510514a8c458a7156e44fc69fdf58` |
| B4-3 incompatible | `0e1823fc1fec804e9c8f7971ed4e8ec2c536defb35cc977225673e05e4c3ef8e` |
| B4-3 trace | `15ff426e46d7b252397611cceecb81f8404199ef88a4317239b28124df199ec8` |
| B5 PG readonly | `07a5d21b09c41279b7cb1838f537718b6ade53bd571e34841f75505157d8fca6` |

## 用量与费用

完整安全摘要：[`round-05-b4-usage-ledger.json`](round-05-b4-usage-ledger.json)。

- 模型 HTTP：3（B4-1 两次，B4-2 一次，B4-3 零次）。
- 已知 token 成本上界：0.004293 CNY；B4-2 迟到 response usage 未可靠返回，保留 1.0 CNY unknown reservation。
- 合同费用上界：6.0 CNY；未超上界。供应商实际账单仍未核对，未知不释放。
- trace POST/GET 不是模型 HTTP；本轮没有超出合同允许的数据出口字段。

## 局限

模型返回的 `reported_model` 为已接受 alias `deepseek-flash`；探针本身是固定 fixture/PG 机制验证，不是 Holmes 真实故障调查。没有把三项结果写成健康、恢复认证、质量通过或 M0 完成；SPEC gate 与 feature passes 保持原状态。
