# 本轮证据与环境收尾独立复核

日期：2026-09-09。受检主工作区 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`，环境分支最终 `ce5e2a2` 已整合为 `ed41def`；本次另覆盖当前 SPEC/C3/首片候选文档未提交修订。审查者不执行模型、trace、目标查询、数据库/虚机启停或通用全量测试，不读取 .env、provider reasoning 或账户余额全文。

**结论：已核查的收尾算术、保全工件和当前停机状态相符；未发现此收尾范围内的阻断问题。** 这不改变 [真实调查失败结论](investigation-outcome-review.md)：两次主动故障调查和最后业务接续均未交付故障最终报告，SPEC门槛仍未打开。PR可以作为证据/修复变更进入审核，不能把PR检查或审查成功说成M0/产品功能通过；远程PR/CI/机器人审查闭环未在本报告认证。

## 费用、次数与时长

独立读取环境原 `holmes-request-ledger.json`、本轮Flash公开原始结果与 `final-usage.json`，用Decimal重算：

- 模型HTTP：Flash4 + Holmes16 = 20；Holmes工具59；本轮trace上传1。到模型请求上限，不能再发起模型请求。
- 输入170078、输出46839，总计216917tokens，全部与原usage之和一致。
- 全部输入按cache-miss峰值3、输出9 CNY/M估算为0.931785 CNY，空闲减半为0.4658925；Holmes按已记录cache-hit/miss分别0.05/1.5、输出4.5 CNY/M，Flash未知cache按miss计算，合计0.3834861 CNY。这是核对合同费率假设下的算术，不是重新认证供应商实际账单/费率适用性。
- final-usage所记余额减少0.39是账户聚合观察，不是各Run可归属账单。本审查未读取或独立重取账户余额；小额差值、显示精度、入账延迟与并发用量不能被当作每案真实费用证明。
- 本轮20 CNY未核账预留 + 旧4 CNY未核账预留 = 24 CNY；预留不是实花金额。旧4是此前未结账历史，并未挤入或扩大本轮20 CNY授权。
- 六个Holmes Run的首模型请求到末响应时间跨度逐项由ledger起止时间重算，与final-usage一致；包含中间工具，不能称完整进程wall time或SLA。

## 导出与停机的实际核查

环境原工作区保留 `tmp/m0-environment/jaeger-final-export`。对公开manifest的19份gzip逐一解压，核对raw SHA256、raw字节数和trace条数；合并traceID恰为2792，压缩文件总9765809bytes（约9.31MiB），各源结果均小于25000上限。导出scope明确为当时Jaeger保留的一小时、逐服务查询；非事务快照，不证明此前没有驱逐或全部历史完整。

已读 `export_traces.py`，固定localhost只读Jaeger端点；输出目录exist_ok=False避免同目录重跑覆盖，每源读取至64MiB+1后超限停止并保留部分工件。该脚本为已授权工程保全，不是模型工具/产品权限；有界读取并不等于任意不合作源的绝对wall-time取消保证。受检源码SHA-256 `d882c6a4258621bfd6cf9bcfa201c4a9809e031f1aa4992923a23e31d76247e3`。主工作区与原环境9个Python环境脚本逐字一致。

`stopped-resources.json`原快照解析为26容器全部exited、6数据卷保留；个别退出码非0，不据此声称所有服务均优雅退出或产品恢复机制通过。随后本审查实际执行只读 `colima list`，default和m0-otel均Stopped；`lsof`在55431、18080、18081、19090、16686、19200未见TCP监听。没有启动已停环境来验证。

另核对 `postgres-cleanup.json` 与当前文件/进程：主任务PG数据目录存在，postmaster.pid不存在，55431未见监听；系统PostgreSQL PID4391仍存在且进程名为postgres。未删除数据，未访问/修改系统PG。

还原和恢复窗口原始数据已在调查审查报告逐项核对：原配置字节恢复，8条checkout200日志与8条无可见错误trace的ID集合一致、5分钟错误增量0、付款恢复。仅是有流量的本次工程还原观察，不是F6/Kubernetes HealthProfile认证。

## 最终文档边界

SPEC仅修正“完全没有实现/部署证据”的过期泛称，明确无产品实施或生产部署证据，Feature implementation gate仍not cleared。C3新增单次Flash固定协议链路的实际状态，同时保留完整协议/恢复矩阵和产品验收未完成；不把Holmes失败写为调查成功。首片候选计划已明确本轮4HTTP/128KiB/8192等只是**尚未通过故障报告链路、不得作为最终冻结值**的校准起点；保留真实目标/依赖授权、动态可见视图、持久步骤恢复与工具wall-time缺口。

实际模型/环境失败、不同窗口、不同输入上限/投影以及最后新Run业务接续均保留原记录；不得重写为匹配条件评测或跨进程provider私有协议恢复。任何后续M0实验须先明确相应合同和可用授权，当前20模型HTTP已耗尽。

## PR #15 历史镜像锁覆盖 P2：独立修复复验

对应Code Review comment3971829340。旧入口会把后来inspect到的tag/digest重新写入已提交image-lock.json/configuration-hashes.json，且此前先写运行proxy；这是历史证据保全缺陷，不改记任何已经发生的实验成败。

独立检查修复：两份docs manifest只作输入，缺失拒绝；先收集全部镜像并严格比对repo digest、image ID、架构和记录内容，再在内存核对全部候选配置hash；全部通过后仅生成tmp运行Compose/proxy。镜像/配置漂移的拒绝发生在任何输出写入前。这里生成的是**与既有归档相同的运行文件**，不是把新运行/新tag重新认证成旧实验。reproduce.md已把原部署/改proxy命令标为历史，并明确不同路径/配置或新实验应另有记录；没有增加通用工件平台。

独立实际运行 `/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_freeze_images.py -q`：**9 passed in 6.54s**。这些测试在临时目录执行真实freeze_images.py CLI，PATH首项是断言参数的fake Docker inspect，未调用真实daemon。覆盖同锁成功且档案非规范空白保持、末个proxy镜像digest/architecture/ID漂移、collector/proxy/Compose漂移、两份历史文件缺失；拒绝时对docs/tmp全部文件作字节快照比对。执行者将平台负例由占位值改为真实amd64后，审查者单独复验该例：**1 passed in 0.93s**。已读取保留的旧版9失败红测工件；未将搭建错误算成产品失败。

另针对实际环境当前归档做只读重放：真实输入配置/历史manifest不变，mock仅替换Docker inspect和最终Path.write_bytes。26条镜像/配置核验通过，恰有2个运行输出写请求被截获、实际写入0。与Git HEAD逐字对照，2份manifest及4份runtime-configuration原件共6文件全部不变。

受检环境工作区源码SHA-256：freeze_images.py=`d0d73303467fef0bf2eabeb143db27bbbd9ab94bd2d02e71f2afa32ad86af71f`；测试=`1701a8958a8030078a2e8a166a9808d8116141424eeccb0c0818b8155e0fe966`。该修复的本地独立验证通过，没有剩余该发现阻断项；不宣称运行文件双写具备崩溃原子性，不启动环境/模型，不替代最新提交CI及已触发Security Review/Code Review闭环。

## 同类 trace manifest 覆盖路径：独立补验

父执行者复现：新checkout保留已提交jaeger-final-export.json但没有tmp导出目录时，旧export_traces.py会覆盖该历史manifest。修复仅在任何目录创建/HTTP前检查manifest.exists()或is_symlink()并拒绝，最终写manifest使用open("x")，避免检查后出现文件时覆盖。历史导出事实与实验成败不变，没有新增平台。

独立阅读旧入口红测：存在manifest分支未拒绝，得到1 failed/1 passed，确实击中覆盖路径。修后独立运行 `.venv/bin/python -m pytest tests/test_m0_trace_archive.py -q`：**2 passed in 0.04s**。临时tree中的真实runpy入口配假urlopen，既有manifest时无HTTP/无导出目录且历史字节不变；无manifest时允许原新导出路径。

另在主worktree通过Python CLI/runpy直接执行实际脚本路径，HTTP和socket入口均替换为拒绝函数：实际既有manifest触发“Historical trace manifest exists”退出，HTTP/network尝试0，原文件字节和tmp导出目录存在状态均不变；没有启动环境或发送真实GET。原manifest SHA-256=`fed1376f6dfc6cfadc8381181aa5189f402e96dd988b9ac8fb20e54382176cd9`，受检修复源码SHA-256=`aee371fce53407f6a50509b276aaa2d1c147c4a374261274deaad3c6459fd55f`。

本路径独立复验通过，未发现剩余该覆盖问题。仅认证本地修复和历史保全；最新提交的Code/Security Review与CI仍由主执行者等结果并闭环，不能以旧提交审查代表当前修复。

## Code Review comment3971972974：全部bind输入保全

旧核验仅覆盖Compose及3个生成文件，没有覆盖实际挂载的flagd、Grafana/provisioning、Collector extras和商品数据。归档Compose共有9条bind声明，其中flagd目录被两服务复用。该发现是重现入口的未覆盖输入，不能据事后修复断言原实验时点已经有完整bind验证。

独立核查本次最小修复：根据已有source-downloads.json中的固定OTel commit URL和归档SHA验证本地otel.tar.gz，按tar的原目录/文件全集对比每个非生成bind源；不新建或更新基线。目录集合包含空目录，新增/缺失/改字节均拒绝；文件、目录、祖先路径和生成路径symlink拒绝。未知来源先按路径分类，原tar不存在的源及目录新增成员在读取其内容前拒绝；FIFO不被作为输入打开。3个生成文件继续受原hash核对。完整候选通过后才可写运行输出，`--check-only`则完全不写。

已读取保留红测工件：12 failed/9 passed，其中旧入口对实际bind/tar漂移或unknown路径未拒绝。首版独立25项通过后，增加源树内未知项早拒绝及对应回归；最终独立执行 `/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_freeze_images.py -q`：**26 passed in 7.83s**。测试用临时tree运行实际CLI、fake inspect，覆盖flagd/Grafana/extras/products、目录新增/删除含空目录、文件/父/祖先/嵌套symlink、源tar漂移、源树内外unknown FIFO（即使测试Compose hash匹配）及check-only无写；没有真实Docker或网络操作。

另外，审查者在原环境worktree对**实际保留tar及当前9条bind内容**执行真实脚本 `--check-only` 路径：只用已归档image记录替代Docker inspect，并将Path.write_bytes/write_text拦截为拒绝。26个镜像记录、9条bind和配置核对通过，实际写0。该步骤证明现在保留的源码树/生成配置与固定归档匹配；没有重新检查已停Docker中的镜像，没有重启服务，不追溯认证过去运行时点。

与Git HEAD逐字核对，image-lock、configuration-hashes、source-downloads以及4个runtime-configuration原件共7文件保持不变。最终受检源码SHA-256=`f0d47cba90b0c46239061b0f016564b542a23c0d8218133212c819647cd0702d`；测试=`0541d29c493f34bc7783c0b01d63a777142c0319a088c0c70c62da7efabab222`。本P2本地独立复验通过，没有剩余已定位的覆盖缺口；不声明敌对文件系统并发下的无TOCTOU保证，不扩产品/恢复平台。远端最新提交的复审与CI仍需闭环。

## Code Review comment3972153923：observation摘要及同组出口复核

父执行者在fresh checkout复现：ignored raw目录不存在时，旧observe_window.py可重写已提交recovery-01-observation.json。已独立读取红测：旧入口未抛拒绝，1 failed/1 passed（另2 deselected），覆盖真实runpy入口而非实现内部细节。

修复前置summary.exists()/is_symlink()拒绝，位于raw目录创建和全部query之前；最终使用open("x")防止覆盖已存在摘要。独立运行 `.venv/bin/python -m pytest tests/test_m0_trace_archive.py -q`：**4 passed in 0.02s**，包含export与observation各自既有档案拒绝/新档案正常路径。

另在主worktree用Python CLI/runpy执行实际observe_window.py，参数指向真实recovery-01摘要，HTTP与socket入口均硬拦截；得到Historical observation exists拒绝，network尝试0、摘要字节和raw目录存在状态不变。原摘要SHA-256=`2f36d80afe9b5df50d7d3eeeda2b245d3e143d65c5a841d3e35b377237bf77aa`；受检源码=`39b342870d98f01e0bd8da6810ed8ec7f1210b3ea4672e1c9a34aae664448ceb`。没有任何真实GET、服务或模型调用。

本次同时扫描并核对 `scripts/m0_environment/` 当前全部9个Python脚本的文件写入路径，未再发现同组docs/evidence不可变摘要的相同遗漏：

- capture：写入已跟踪证据label目录；mkdir(exist_ok=False)在HTTP/命令采集前独占目录，已有label拒绝。
- freeze_images：docs下3份manifest只读，输出仅候选tmp Compose/proxy；全bind输入验证沿用上一项审查。
- export_traces：manifest前置存在/symlink拒绝，tmp目录独占，最终manifest open("x")。
- observe_window：本次补齐tracked摘要前置拒绝及最终独占写；raw目录仍独占。

其余prepare仅生成可变tmp配置；Holmes每Run目录独占，Run内观察/共享allocation账本是按设计更新的运行记录（allocation锁与原子保存），不作为新的docs历史摘要入口；development_fault的原始/注入快照有存在拒绝、还原校验注入字节、时间线append；holmes_wire_check只输出离线检查结果，read_proxy不向docs写文件。该扫描限定这些已知脚本，不认证任意外部shell重定向、敌对文件系统竞态或未来新增入口。

此P2本地独立复验通过，原实验成败和档案均保留；仍须等待覆盖最终提交的Code/Security Review与CI，不以本报告替代远端门禁。
