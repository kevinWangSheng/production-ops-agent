# M0-02 saved报告执行前独立核验

日期：2026-09-10。范围：仅report phase/max_steps=1、既存业务views新Run。**该范围本地守门核验通过，可由已授权父执行者执行一次saved请求；不代表真实请求或报告质量通过，不覆盖active身份/工具scope，也不开放SPEC实施入口。**

审查实际round02.py、transport_worker.py、run_bounded.py、holmes_baseline.py和主实验round-02-contract.md。未读.env或private-protocol；0模型/trace/余额请求。

曾发现异常usage只保留预留却允许跨phase请求；实现者已补全轮持久拒绝。独立临时ledger用prompt_tokens=1048577超假设usage，重开Budget后normal phase reserve明确ValueError invalid usage requires review。未知请求占用保留，旧24CNY未触碰。trace当前0上传。

6项定向tests通过（0.73s），覆盖信封/私有字段不改、未知预算重启、已知上界只结算本轮、异常usage留额、业务导出白名单、忽略TERM子进程强杀回收。另独立测试跨phase拒绝及双份32768字符最高JSON转义unicode响应786500bytes，小于2097152限额；这只是编码压力检查，真实供应商响应仍由流式byte guard限制。

真实固定Holmes环境 preflight命令：
`tmp/m0-environment/holmes-venv/bin/python scripts/m0_environment/holmes_baseline.py --run-id m002-independent-preflight-01 --question-file tmp/m0-environment/holmes-inputs/m002-saved-report-01.json --phase report --max-steps 1 --preflight-only`
结果import_configuration_pass；新工件目录tmp/m0-environment/holmes-runs/m002-independent-preflight-01。preflight不读凭据、不实例化真实模型客户端且拒绝模型egress；这是导入/配置证据，不是实际wire调用证明。

静态核验：report必须max_steps1、空toolset、本地证据GET拒绝；模型固定Flash thinking/high，发送前持久全轮/phase预算，禁止重试，HTTP只允许明确DeepSeek地址且禁redirect/env proxy。HTTP子进程受360秒/绝对deadline限制并有限TERM/KILL清理，Run外层supervisor清理进程组；必须按合同用supervisor发起。provider响应仅独立private目录写入，业务交付仅user/tool字段，事件不整包导出，只有stop非空正文记returned。费用usage不是账单。

剩余边界：真实报告与证据因果质量待独立评审；active模式混合来源/metrics授权、raw/view绑定另审；PG步骤审查4项正在修复，saved不依赖该模块。此结论只覆盖以下文件hash快照；后续实质变更需复验。

- `scripts/m0_environment/round02.py` SHA256 `24a1c33555a64419e62876065183e3dc04cf25734876d8694197bc687e4aad76`
- `scripts/m0_environment/transport_worker.py` SHA256 `31e7db6eddbf6917b3409cb73d28c267c594b151b33f7ddb01077e5470e93ca2`
- `scripts/m0_environment/run_bounded.py` SHA256 `c46493aa1c44a4e5a0f627a62525d1ce82215dbc73dc76405fa4bd40e0550d24`
- `scripts/m0_environment/holmes_baseline.py` SHA256 `5fab38beecbb7f38f3078ae8866e096006f93adf884be1410dee83d6c1a9126e`

## 付费前最终快照复验

父执行者发现候选hash变化后暂停发起；独立重读saved实际路径：新增api.deepseek.com仅443端口限制、显式检查上游compaction关闭、full-wire cl100k参考估计98304守门，active registry/projection新增分支在report/max_steps1不执行。估计不改变保守供应商上界预留，也不删改协议。10项定向tests通过（1.44s），第二次真实Holmes preflight m002-independent-preflight-02 返回import_configuration_pass。saved范围结论继续成立，0模型；active范围仍另审。

最新覆盖hash：holmes_baseline.py `ee0341da5ea34a7d00dc5daaa51df4236c0b44d95dec3bd0c73a0292faa96a6d`；transport_worker.py `4798c394bbdd035f4615b38846d31115f9aead0e19b0580ff5d4c99ba0b26057`。round02.py与run_bounded.py沿上方hash未变。

## 首次真实失败与诊断保全修复独立复验

实际m002-saved-report-01为1模型HTTP、0工具、0trace，result为failed/InternalServerError、boundary_errors仅RuntimeError；交付记录留HTTP200，账本3.44064 CNY usage/cost_upper均null。本轮未读取实际private目录，返回model具体值仍unknown。结合当时200后显式model guard可推测identity mismatch，但不能写成已知provider实际model值。首请求占用不释放。

修复capture_response先将200原始bytes base64精确保存在同Run/provider受限目录（700，文件600），再解析并写明确业务白名单诊断（model安全字符、整数usage、finish枚举、完整业务content）；identity mismatch仍抛ModelResponseDenied，不放宽Flash profile，不将拒绝报告标为accepted。malformed JSON同样先保全再固定错误码拒绝。wrapper在SDK包装异常前保留该固定码。

独立13项定向tests通过（1.45s）；另临时合成异名响应probe验证：身份拒绝、精确raw bytes可还原、完整SYNTHETIC_REPORT业务正文保留、SYNTHETIC_PRIVATE未导出、文件权限600；malformed JSON有拒绝与业务诊断。只读取临时合成私有fixture，未读取任何实际provider private正文。该修复局部核验通过，可在父执行者先落盘足够子额度后按已授权第二HTTP验证；不保证报告返回，不释放首次unknown。

该修复覆盖hash：round02.py `f87d6e2c36de336f8991804f04cb1c6dccd37217c36c072668820038e0784960`，holmes_baseline.py `0d1fe1bc658a997fd2b40897e42ac501dc05b28f5e0c0d8488f3f363df591609`；worker `4798c394bbdd035f4615b38846d31115f9aead0e19b0580ff5d4c99ba0b26057`、supervisor `c46493aa1c44a4e5a0f627a62525d1ce82215dbc73dc76405fa4bd40e0550d24`未变。
