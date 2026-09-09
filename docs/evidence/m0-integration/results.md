# 协调者汇合自测

对象：依赖快照51c892a及verification.json逐文件hash的本地集成改动。A/B/C已分别完成fresh独立审查与发现修复；本文件只是协调者自测，不是独立集成认证。

- make check：135 passed，13实际数据库测试默认skip。
- M0_B_POSTGRES=1 pytest tests/integration：12 passed，1原生数据库restart跳过。3项新汇合覆盖正常两轮账本结算+trace替身回读、断流unknown跨Run不增额度、数据库连接失败不发送。B的实际数据库restart已有独立运行证据，不因这里skip而冒称本次执行。
- Gitleaks8.30.1：安装脚本从官方发行源下载并核验固定SHA256，真实binary合成泄漏自检/干净样本通过，Git历史及暂存后已跟踪文件扫描通过。扫描输出固定，不读取.env；snapshot单测验证误跟踪.env在读取前拒绝、symlink拒绝、untracked私有文件不复制。Git扫描不代表运行时全部出口安全。
- live：退出3，LIVE_NOT_ENABLED，0模型调用/0trace上传/0实际费用。
- B同一专属PostgreSQL17.9被顺序复用，结束已stop，数据保留；没有创建第二个本地cluster。

## 失败及处置

首次新测试夹具把usage-only SSE块错误地带choices，适配器正确拒绝；另错误访问History不存在的groups属性。2 failed/1 passed原始日志见integration-first.txt；修正合成SSE usage块为choices=[]，使用公开messages()断言后3 passed。未修改A实现迁就测试。

一次全数据库测试因协调者过早停止专属PG失败（命令仍在安装扫描器，后续测试尚未开始）：1 failed/11 errors/1 skip，错误均STORAGE_UNAVAILABLE。已纠正调用顺序：单独start，等待完整验证子进程返回，再stop；完整12项集成重新通过，见verification.json。不把环境操作失误称为代码缺陷或隐去失败。

Docker buildx imagetools本机入口不支持所请求选项，未启动daemon；改用Docker官方registry读取postgres:17.9 manifest，固定index sha256:2a0d0fe14825b0939f78a8cad5cd4e6aa68bf94d0e5dd96e24b6d23af4315545，amd64 sha256:66b6a97eac1771fc78bd201b918b4253859f436c6913aeede97bd5366cce89ae。CI容器尚待首次运行；只用合成trust身份，不注入业务Secrets，1CPU/512MiB，不授权真实额度或产品鉴权。

下一步独立集成审查、修复和有界PR/CI；真实模型+trace、账号区域范围、费用期限授权、M0退出和产品验收仍未执行。
