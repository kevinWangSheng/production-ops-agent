# M0-02 首次 saved 响应保全缺口

2026-09-10。仅本地诊断与修复，无新增模型HTTP、trace上传或环境操作。

`m002-saved-report-01` 的业务工件显示一次请求收到HTTP200，此后异常为RuntimeError，经SDK包装后最终InternalServerError。旧逻辑在检查response model后才保存响应，在该位置的显式RuntimeError只有model identity mismatch；这是源码控制流证据，实际响应model字符串没有保全，无法恢复，不能据此放宽模型身份。原请求账本3.44064CNY未知全预留、原失败与旧24CNY均保留。

修复在任何JSON/model验证前，将完整精确响应bytes以base64存同Run `private-protocol/response-N.json`（目录700/文件600，不导出），再保存仅允许字段的`response-N-business.json`：model、usage、finish、content、HTTP状态、identity_accepted及固定拒绝码。模型异名仍拒绝，不将该正文当已接受报告，也不按Flash价格结算异名usage；账本保留收到的HTTP状态、observed_usage_unsettled与拒绝原因。JSON不可解/顶层非object也先保全。私有推理不入业务投影，合法model的完整协议原样返回上游。

定向验证：主仓库`.venv/bin/python -m pytest tests/test_m0_holmes_round02.py -q`，13 passed；新增异名响应拒绝前保全回归先失败（helper不存在），实现后通过。原真实失败不重打；合法/异名/不可解JSON、私有不导出、预算未知持久、异常usage跨phase阻断、子进程terminate/kill与supervisor组清理均有离线检查。Ruff check/format通过。独立reviewer另复核13项及真实Holmes import preflight。

父执行者要求为第二saved冻结运行文件，SHA256：

- holmes_baseline.py：0d1fe1bc658a997fd2b40897e42ac501dc05b28f5e0c0d8488f3f363df591609
- round02.py：f87d6e2c36de336f8991804f04cb1c6dccd37217c36c072668820038e0784960
- transport_worker.py：4798c394bbdd035f4615b38846d31115f9aead0e19b0580ff5d4c99ba0b26057
- run_bounded.py：c46493aa1c44a4e5a0f627a62525d1ce82215dbc73dc76405fa4bd40e0550d24

剩余边界：响应bytes超限的partial保全、200 error envelope专用固定码及更多组合测试尚未实现，不能声称已覆盖。第二saved沿上述已审快照，参数不变；phase调配由父持锁修改并同步其合同，诊断者不改费用账本。active实际来源范围/精确file bytes hash修复仍待第二saved返回后继续。

后续本地修复（第二saved结束解冻后）：新增response byte超限/流读取异常的受限prefix与complete=false、固定错误码；非200及200 error envelope保全；异常finish_reason类型不会阻止业务投影；usage异常最终持久blocked及固定拒绝诊断。31项定向测试通过。以上先前“剩余边界”是13tests快照的历史状态，当前active候选已送独立复审，真实验证另记。
