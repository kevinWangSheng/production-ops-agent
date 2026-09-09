# HolmesGPT 基线实际执行

最终状态（2026-09-09）：**已停止所有Holmes模型调用；5次开发Run中只有normal-03返回最终业务结论，四次失败原样保留，两个故障Run均无最终结论。不是整体通过。** 固定上游真实调用15次HTTP，均响应200，另有本地包络拒绝未发HTTP；59次工具查询。normal-03质量仅有限支持，不能认证健康；故障定位能力本轮未证明。

当前累计分配16HTTP/16CNY，实际15次，剩1次不使用；上游trace上传0。实际response model全部deepseek-v4-flash，input141487/output38443 tokens。按当前已知cache字段估算峰值0.6056352 CNY、半价offpeak假设0.3028176 CNY；更保守全部cache-miss峰值上界0.770448 CNY，**均不是已核账费用**。详细字段及计时口径见`holmes-per-run-usage.json`和`holmes-usage-summary.json`。


历史记录，截至 normal-02：已实际执行固定上游循环和官方Flash多轮工具调用，两个正常窗口开发Run均没有最终结论。失败保留，不算质量通过。

| Run | 实际模型HTTP | 工具查询 | 结果 | 证据边界 |
|---|---:|---:|---|---|
| normal-01 | 3，均200 | 11 | failed / InternalServerError，无最终结论 | 未留具体包络拒绝码，不能把SDK异常名当供应商HTTP500，也不能回填后续Run的确诊 |
| normal-02 | 2，均200 | 8 | failed / InternalServerError，无最终结论 | 第三次发送前包络83148bytes超过65536byte本轮限制；thinking/model/max_tokens均符合；本地拒绝不计实际HTTP |

两个Run固定相同 `1788975149.661..1788975449.661` 窗口和同一问题，normal-02增加确定性trace投影及安全出站包络诊断，同时只读proxy开始提供Prometheus官方错误细节。不是同条件纯质量比较。

已证实复用缺口：normal-01真实checkout trace返回508KB，原上游 `spill_oversized_tool_result` 在无bash情况下把超限data清空并返回ERROR；完整原始观察落盘并不代表调查模型看到完整trace。normal-02投影在真实normal-01数据上复算为7 traces/376 spans，7587 bytes、20展示/356省略，保存全部真实观察hash、trace/spanID及按service聚合；不代表全体时段或均匀样本。独立Agent复核聚合与引用对应。

模型自述不是正常性权威；独立可见观测包含错误增量0与较长checkout RPC时延，不能强行把控制窗称为全健康。两案无最终业务输出，尚无质量合格结论。

原始私有业务工件：`tmp/m0-environment/holmes-runs/{run}/`（configuration/input-business/observations/result-business，normal-02另有tool-model-view与request-envelope-checks），累计实际HTTP和usage：`tmp/m0-environment/holmes-request-ledger.json`。没有provider reasoning正文导出或trace上传。前5HTTP总input26621/output12510，峰值cache-miss费用上界0.192453 CNY，非实际账单；费用以最终账本为准。

## normal-03：已返回业务结论，质量另审

最小修复后，128KiB包络容纳真实多轮历史，累计额度按主Agent记录调整为16HTTP/16CNY（整轮20HTTP/20CNY不变）。4次HTTP全部完成、14个只读工具查询，最终finish_reason=stop、status=investigation_returned；模型结论与证据正确性由独立Agent核查，不以该状态表示产品通过。

新窗口 `1788975849.645199..1788976149.645199`，问题结构不变，实际清单补RPC metric labels。模型报告checkout/payment没有发现退化，依据错误率/调用时延和trace；披露payment RPC histogram与OTLP logs缺测、样本偏差、counter30是累计而非窗内数量，并指出recommendation有非零error率。所有判断仍以原观察对照，尤其healthy措辞及缩略e2等引用需独立审查。normal-01/02失败未覆盖或移除。

实际model-visible业务投影保存在normal-03每个`*-tool-model-view.json`；全量观察另存observations.json，不能把省略部分说成模型已读。故障案前仍需独立工程注入/观察给出的不含答案窗口。

normal-03独立结论复核：主要引用数值准确；e5/e6为同5条trace，去重236 spans。质量不足保留：`healthy latency envelope`没有冻结SLO依据；`unbiased span error metric`尚未证明采样/覆盖；约45是span增量估计，不是去重calls/orders；缩略(e2)可由人工映射到normal-03-e2，但尚未满足机器直接解析完整引用。累计30的时间窗口限制及日志/直方图缺测已正确说明。原业务输出不改写，分类为“真实链路返回、质量有限支持”，不作为产品健康认证。

## fault-01：真实症状调查未完成，日志体积适配失败保留

固定开发窗口 `1788976626.533447..1788976926.533447`，模型只收到POST /api/checkout HTTP500症状及既有来源清单，没有注入参数/答案。3个实际HTTP均200、14工具查询，第四个候选请求131799bytes超131072上限，在HTTP发送前拒绝；最终failed/InternalServerError，无最终业务结论。错误名不能当供应商HTTP500。

实际工具查询将边界截成整数 `1788976626..1788976926`，早0.533447秒；本轮proxy未系统强制精确冻结窗，只执行实例/服务/最大一小时范围，不能宣称通过完整时间范围权限验收。

已确认额外体积来源：frontend-proxy日志25.5KB、cart日志20.5KB含重复索引/资源等metadata。最后fault-02复验只加确定性日志视图：完整raw保留；每条显示的body不改、保留document ID/time/service/severity/trace/span及错误字段，完整resource/scope提升到包级身份表、逐条reference，不丢container/integration/version。真实fault-01输入复算frontend20/20展示12957bytes、cart20/20展示8484bytes；命中/返回/省略数及hash另存。128KiB/8192/4HTTP限额不变；在父任务原累计16HTTP/16CNY内，最后最多4HTTP后停止模型，不再提高上限。


## fault-02：最后复验仍失败，模型调用停止

最后4HTTP上限内实际发3次，均200；12工具查询均200。第四候选包络158029bytes超过131072，在发送前拒绝；原result-business仍failed/InternalServerError且没有最终业务结论。实际查询保留本案小数窗口，与fault-01的截整偏差分别记录。完整日志投影经过独立字段级复算仍不足以约束多轮总历史，不能因此宣称故障被定位或只缺“输出格式”。

当前证据支持的未解决问题：本轮自建总字节包络限制与上游按token的上下文管理未对齐；工具体积投影只控制单条结果，不保证全部工具历史及同Run协议续传后的下一请求适配预算。两次故障均未返回报告，效果评估保留失败。后续需要在冻结权限/费用下验证总上下文管理和有界终止语义；不得删除同Run必需私有字段、不断抬cap或擅自增加调用来制造成功。本轮已停止模型，环境者随后执行独立工程恢复，不由Holmes执行或认证。

## 检查与复核范围

两个脚本最终Ruff check、Ruff format --check和py_compile通过；离线真实LiteLLM协议构建/续传布尔检查与token计数器初始化通过（不是额外live模型）。74个私有业务JSON检查未发现实际模型key字面值或provider reasoning_content/provider_specific_fields/thinking_blocks字段；该检查不证明所有网络路径或OS沙箱。

原始业务JSON+累计账本共75个文件hash在`holmes-private-artifact-manifest.json`，原始数据保持ignored私有文件，不在Git发布。清理工作树前必须安全保留这些工件，不能只保留hash后删除原始来源。

独立上下文Agent已复核执行器边界、真实投影字段/计数及最终结果；主任务工作区`docs/evidence/m0-real-investigation/environment-boundary-review.md`和`investigation-outcome-review.md`持有完整审查。最终运行器hash `aaed9ac3ae501a2271c60bd52c5beb5f2b88611c633d8d0a808dbaa08508ead8`；不以审查替代实际权限隔离、完整M0、产品验收或生产证明。
