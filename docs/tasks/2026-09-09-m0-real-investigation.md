# M0：从协议链路进入真实调查

日期：2026-09-09；状态：进行中。目标为取得首个产品纵向流程所需真实环境/上游/协议证据，依据SPEC实施门槛、C3、M0计划及F1/F2/F3/F7/F8/F12/F14。产品实施与passes未打开。

## 工作区与合同

主工作项复用production-ops-agent-m0-01 / chore/m0-real-investigation，干净旧分支快进已合并main738b5c7。独立环境production-ops-agent-m0-environment / chore/m0-real-environment从同基线建立；环境Agent唯一负责OTel实例，HolmesAgent仅负责基线配置/运行，避免重复搭建。

[本轮绝对截止、授权、预算和完成条件](../evidence/m0-real-investigation/contract.md)。新增20 CNY/20模型/5trace，截止2026-09-10T17:14:30Z；旧Pro+首次Flash4 CNY未核账保留。原数据库在原worktree，系统PG不动。

现场GitHub：PR14 mergedAt=2026-09-09T17:05:55Z、merge738b5c7d637ded49919d6368fbc3980b1b9c5856；main CI34380825700成功，本地主仓库main/origin/main一致且干净。原专属PG停机后经所属脚本重新启动；历史归属和22条等旧实验状态按实际全表行hash保留。未清理任一旧worktree。

## Flash真实链路

[原始安全结果](../evidence/m0-real-investigation/flash-results.json)保留两次新实验：

1. 当前请求诊断：2请求、3.453秒，最终69字符为Markdown json围栏，内部target/evidence_id严格匹配，整体json.loads失败。业务failed/LIVE_FINAL_JSON_INVALID；0上传。只能定位这次，不倒推旧轮丢失正文。
2. 最小修复后：仅最终请求加入response_format=json_object，其他请求/fixture/成功校验不变。2请求、10.792秒，最终57字符严格JSON；PG业务completed及诊断、outbox可回读；1上传/2回读后TRACE_VERIFIED。

官方JSON模式依据：https://api-docs.deepseek.com/guides/json_mode/ 。[诊断1](../evidence/m0-real-investigation/final-diagnostic-1.json)、[复验2](../evidence/m0-real-investigation/final-diagnostic-2.json)。诊断包装器只记录结构和已确认等于已知fixture的最终业务文本，不保存reasoning或未知正文；其[原始脚本](../evidence/m0-real-investigation/diagnostic-harness.py)和私有运行目录保留，源码digest另列。Python/依赖沿用锁定CPython3.12.13、OpenAI3.10.0/HTTPX2 2.12.0/LangSmith0.12.2；真实请求直接HTTPX2，未宣称OpenAI SDK已真实运行。

回归测试捕获第二次请求JSON mode，同时返回真实围栏文本仍严格拒绝且不上传。有效修前红例见[输出](../evidence/m0-real-investigation/json-mode-red-valid.txt)；前两次测试搭建错误保存在私有目录，不算有效回归证据。修后75项定向通过、make check265 passed/17默认PG skip；[完整输出](../evidence/m0-real-investigation/check.txt)。独立源码/PG/原始证据审查待收尾。

本轮Flash合计4模型、1上传，输入1973/输出205 tokens；峰值cache-miss估算0.007764 CNY，当前空闲时段估算0.003882 CNY。实际账单未核；4 CNY新子额度继续占用，加旧4 CNY共8 CNY未核账。Holmes已分配12 CNY/12模型/1trace，累计已分配16 CNY；剩余4 CNY/4模型/2trace未分配，不是已支出。

## 后续与完成条件

环境正常遥测/只读权限→正常与故障上游实际调查→独立核对→冻结首个纵向流程验收/环境与恢复前提；条件不足列具体缺项，不改功能passes。

收尾需整合环境/基线证据及预算，形成入口决定和下一实施任务，更新ROADMAP/M0当前状态，完成独立审查、PR最新CI及已触发review；不自动合并。停止本轮服务，所有数据库/证据保留。
