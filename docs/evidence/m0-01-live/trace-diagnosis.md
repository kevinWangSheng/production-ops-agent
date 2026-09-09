# LangSmith 回读误判：根因、修复与真实响应复验

2026-09-09。用户指出不应在未诊断原因时停下，当前工作范围是继续只读诊断已有trace、完成本地修复/回归/独立审查并更新原PR；不重新请求模型，不追加上传。

## 实际观察与确定性复现

对原Run只读GET一次，HTTP200；原始响应仅存任务ignored目录0600文件，公开摘要见[trace-diagnosis.json](trace-diagnosis.json)，保留原始文件hash。Run、既有项目身份、inputs、outputs及DTO值类型全部匹配，旧校验唯一失败项是`not returned.get("extra")`。真实extra为：

```json
{"metadata":{"ls_run_depth":0}}
```

固定SDK的内存出站序列化不含该非空extra；原出口也明确拒绝非空extra。因此本次观察支持该字段为平台返回的运行层级装饰，并非模型或应用上传的额外内容。完整execute链路的MockTransport加入同样结构后，先复现`unknown != verified`；修复后同一测试通过。原始真实回读响应在禁socket环境重放新校验器，结果`TRACE_VERIFIED`。

初始诊断区分三种假设：校验误拒平台元数据（通过旧校验唯一失败项及最小复现支持）、出站意外附加数据（固定SDK出站捕获及出口拒绝条件排除）、业务DTO不一致（真实身份/inputs/outputs/type逐项核对排除）。首条执行第二次回读正文未保留，所以不声称逐字重建当时所有响应或确证其首次404；当前同一trace的真实响应已经足以证实并修复校验兼容缺陷。

## 最小修复与边界

- 出口规则不变，仍拒绝非空extra，不允许provider私有字段或任意metadata进入上传体。
- 回读新增`trace_readback_code`：仍严格验证身份、inputs、outputs和值类型；只额外允许exact `extra.metadata.ls_run_depth` 为int0。未知字段、其他depth、bool冒充0、嵌套私有字段均拒绝。
- 2xx JSON null不再与404共享None结果，固定报`LIVE_TRACE_RESPONSE_INVALID`；只有真实404会进入下一次回读。
- CLI结果增加固定`trace_code`，区分身份、输入、输出、额外字段及传输失败；不输出异常正文、响应正文或任意平台metadata值。数据库原记录与原失败快照不改，不伪造第一次执行退出0。

## 验证及当前结论

28项定向测试通过（含完整链路正/负例），完整make check结果见[trace-fix-check.txt](trace-fix-check.txt)。独立审查见[trace-fix-review.md](trace-fix-review.md)。本轮真实新增请求仅一次只读GET，模型/上传新增均0；没有启动PostgreSQL或重新claim已使用授权。

原首条运行CLI退出1、trace unknown的历史保留；后续只读观察确认平台已有正确的业务DTO，修复后的校验器对实际响应验证通过。可以确认原模型两轮和已存trace内容的对应关系，不能将“修复后真实响应离线重放”称为“新代码从模型到上传完整重新运行”。完整M0、其他协议矩阵、恢复导出及产品实施仍未通过。

此前把单次实验的禁止重跑误解为连只读诊断也需等待，是执行边界判断错误；只读排障应继续，新增模型/上传才单独受其调用范围约束。
