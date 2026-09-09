# M0-01 normal-1 入口交付证据

本页为入口准备的历史交付快照；后续用户已授权并执行一次真实实验，当前状态见[执行记录](execution.md)。

2026-09-09；本轮完成范围：首条真实调用入口的本地准备、测试、独立审查和PR交付。实际付费实验未授权/未执行，SPEC实施门槛与全部passes不变。

## 可核查结果

- 基线：本地main/origin/main均ad99e6a，#4–#11合并，main CI 34330344820成功。复用原M0-01工作区，新分支chore/m0-01-live-entry；原main和其他worktree未切换/搬运WIP。
- [逐命令记录与版本hash](verification.json)：make check退出0，211 passed / 15显式PG默认skip；新增专项PostgreSQL两项实际执行通过；默认live及真实私有配置+未批准草案在socket禁止下均退出3、零HTTP。原输出见check-0.txt/check-1.txt/postgres.txt。
- [独立审查](independent-review.md)：全新上下文Agent主动反例与源码核对。期限race修复并复验0HTTP；mock21项及真实PG2项通过。反例额外验证回读布尔类型污染、续接请求超限计数、压缩响应体积约束。
- 固定Gitleaks8.30.1安装与工作树/索引/历史扫描通过，未扩大allowlist。提交前staged扫描再次通过。
- 私有配置：通过专门程序只读两key存在状态，不打印/复制值到聊天/工件。浏览器已有登录，US/default项目及免费套餐只读核对；一次免费项目metadata GET返回200，默认workspace/key与项目身份匹配。主.env只追加已核实的非秘密字段，0600保留；费用/期限无授权，private approval-draft仍approved=false。没有API key创建、workspace/project创建、套餐升级、充值或外部通知。
- DeepSeek用量页显示CNY计费、现有余额足够建议2元；key的模型调用能力未测。约9月10日起v4-pro别名转路由公告已写[方案](plan.md)，实际运行前需重新核查。响应model仅以有限枚举/unknown存入本地记录，别名不能证明底层版本。

## 实际外部动作和限制

模型调用=0；trace上传=0；脚本显式项目metadata GET=1（免费只读，浏览器页面内部请求另属只读UI核对）；其余联网是GitHub、官方资料、工具下载及账号浏览器只读页面。实际模型/平台费用未发生，不把本地测试中的2元合成预留当真实消费。

当前只完成单进程正常链路入口，自动trace恢复、私有协议持久恢复、原各例3次重复、流式/故障矩阵和产品实施均未完成。整个有效HTTP期限420秒，数据库失败保存/关闭仍按有界连接/语句超时处理；无精确420秒进程退出保证。真实LangSmith后端runs回读兼容尚未知，失败会保留业务/outbox并交接。

本任务专属PostgreSQL在本worktree/tmp/m0-b/postgres，端口55431，沿用已核验本地lab脚本，旧B工作区数据库未改。测试前确认端口空闲才初始化/启动；收尾已确认server stopped、保留数据库和私有草案，不清理唯一证据。PR未合并，worktree保留供用户审核与后续获批实验。

## PR与服务收尾

[PR #12](https://github.com/kevinWangSheng/production-ops-agent/pull/12)已创建，代码提交3309daf的checks与m0-postgres均SUCCESS（run34334853120）；独立审查、staged秘密扫描及本地验证见上。后续仅文档交接提交的最新checks以PR实时状态为准。PR保持OPEN，未获得合并或费用授权。任务worktree干净提交后保留；PostgreSQL已确认停止，私有草案/数据保留。

## 下一步

用户审核PR并决定[2元、截止时间与实际模型路由](plan.md)。实际调用前助手复核最新CI/代码摘要、账号免费额度及官方路由，依据真实授权完成私有合同/实验配置；不把PR通过或合并当费用授权。若指定固定0813而别名已转路由，则停止提出不兼容，不静默替代。
