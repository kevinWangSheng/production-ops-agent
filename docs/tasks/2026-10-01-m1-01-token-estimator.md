# M1-01 后续项：上下文估算改用真实 tokenizer

- 状态：已完成（对账推翻「少算 1.6 倍」前提；用户 2026-10-01 决定不改代码，合同第二步取消）
- 更新日期：2026-10-01
- 依据：[整视图计量记录](2026-09-29-m1-01-view-bytes-timeout.md)「另开一项」（估算约少 1.6 倍）；用户 2026-09-29 决定另开查证、2026-10-01 决定按上游改用真实计数；上游 `holmes/core/llm.py` 用 `litellm.token_counter` 逐条计数并按消息缓存
- 工作区：`feature/m1-01-token-estimator`，`../production-ops-agent-token-estimator`

## 问题

`estimate_tokens`（`opspilot/investigation/context.py`）按规范 JSON 字节 × 0.25 估算，真实约 2.5 字节/token，低估约 1.6 倍（推断，未直接测）。校准因子只在响应后上调，压缩阈值 0.95×1M 在一轮内滞后，并行工具结果可能在校准前越过上下文上限。

## 第一步：对账（先做，结果写进任务记录）

用仓库内已有真实 Run 的 ledger（`docs/evidence/` 下含 request 与 `usage.prompt_tokens` 的记录，至少 10 个请求，覆盖小/中/大上下文）离线计算：旧估算、vendored tokenizer 计数（`opspilot/tools/tokens.py`）、供应商 `prompt_tokens`。给出比值表。若 tokenizer 计数与 `prompt_tokens` 偏差超过 ±10%，停下写入待决，不进入第二步。

### 2026-10-01 执行结果

- 对账脚本与结果：[reconcile.py](../evidence/m1-01-token-estimator/reconcile.py)、[README.md](../evidence/m1-01-token-estimator/README.md)、[reconcile.json](../evidence/m1-01-token-estimator/reconcile.json)。
- 扫描 `docs/evidence/` 的 JSON/JSONL：`0` 条同时保存完整 `messages`、完整 `tools` 与 `usage.prompt_tokens`；`1,211` 条只有 usage、request hash/bytes 或摘要（2026-10-01 按 Codex 审查在提交本目录证据后重新生成清单：1,244 条，>500k 9 条；增量来自本目录 live 结果，仍无完整请求体）。`m1-01-view-bytes-timeout` 有 `661,798`、`975,182` prompt-token 的大上下文 usage，但没有请求体；`m1-01-loop-long-horizon/compaction-smoke.json` 只有角色名摘要并注明正文未保留。
- 因此旧估算、vendored tokenizer 计数、两个比值和每消息开销均不可计算；没有伪造值，也没有真实模型/网络调用。
- **待决：**补存至少 10 个真实请求的完整 `messages` 与 `tools`（覆盖小/中/大，最好含 >500k），并与同一请求的 `usage.prompt_tokens` 绑定；补齐前停在第一步，不进入第二步。当前无法给出偏差范围或 ±10% 判断。

## 合同（第二步）

1. `estimate_tokens` 改为用 vendored DeepSeek tokenizer 对每条消息（及 tools 数组）计数，加固定的每消息开销；开销值由第一步对账定出并写一行理由。
2. `Calibration` 机制保留（只升不降）。
3. 计数器缺失或加载失败时 fail-closed（与 #64 工具计数一致），不静默退回字节估算。worker 启动时预加载计数器（同时覆盖 fixture profile，#64 遗留可选项）。
4. 性能：同一消息内容不重复计数（按内容哈希缓存，或等效方案）；一次 1M token 级上下文的估算耗时写进任务记录。
5. `context_policy_revision` 随之变化，旧修订下未完成的 Run 按既有规则处理。

## 验收口径

合同测试：已知文本的估算等于 tokenizer 计数加开销；压缩在估算越过阈值时触发；计数器缺失时拒绝而非退回。一次有界真实 Run，记录各请求估算与 `prompt_tokens` 的比值（期望在 ±10% 内，观察）。

## 不做

不改 25k 单工具上限、不改压缩算法、不改 1M 窗口与 0.95 阈值。

### 2026-10-01 第一步对账续做

- A：用 `m1-01-e-class-attribution/scripts/rebuild_offline.py` 重建 6 条请求并与同一 ledger 的 `usage.prompt_tokens` 配对；`normal-2`、`fault-2` 的 `request_sha256` 精确匹配，另 4 条因导出时历史 reasoning 占位符而 hash 不匹配，均在证据表标注。
- B：使用项目既有 `M0_ENV_FILE` 读取和 `deepseek-flash`，`max_tokens=1`，共 9 次（新增 6 次，未重复中断前 3 次），覆盖约 1k/10k/100k/300k/600k/900k，带 tools 与不带各有样本。
- 证据：[README](../evidence/m1-01-token-estimator/README.md)、[`reconciliation.json`](../evidence/m1-01-token-estimator/reconciliation.json)。prompt 合计 3,023,328，completion 合计 9。
- 结果：A 的 tokenizer/prompt 偏差约 +1.8%–+6.8%；B 为 +4.3%–+26.1%，超出 ±10%。全体中位拟合每消息开销为 -1,913.8 token，说明当前规范 JSON 计数与供应商序列化未对齐，不能把该值作为产品开销。
- ~~待决并停下~~（lead 复算更正）：A 的 tokenizer/prompt 实为 1.12–1.16（执行者初稿误写 +1.8%–+6.8%），旧估算/prompt 为 0.81–0.86。

## 结论与用户决定（2026-10-01）

旧估算少算约 14–20%（不是 1.6 倍），首个响应后的校准即补齐；改用 tokenizer 需先对齐供应商消息序列化，否则多算 12–26%。用户选择不改代码、记录收尾；合同第二步取消，不写合同测试。详见[证据 README](../evidence/m1-01-token-estimator/README.md)「结论」。
