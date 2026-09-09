# M0 真实环境与上游调查准备

日期：2026-09-09。工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment`；分支 `chore/m0-real-environment`，起点 `738b5c7`。

目标：先建立固定 OTel Demo 的正常日志、指标、trace 和身份映射，再实施专属环境中的可逆开发故障，准备固定 HolmesGPT 真实只读调查。依据 SPEC、C3 第 3/8/12 节、M0 计划第 4/5/7 节及 F1/F3/F7/F14；不开放产品实施门槛。

用户授权：核对归属后启动本项目专属本地环境、固定依赖/镜像下载、可逆受控故障。不得影响其他项目或生产。整轮共享20 CNY/20模型请求/5 trace，截止2026-09-10T17:14:30Z；旧4 CNY占用保留，由主 Agent 统一管理。本执行者现阶段模型/trace配额为0，不读取或复制主仓库.env。

## 实验合同（操作前）

问题：当前16 GiB宿主能否运行固定版本 OTel Demo 并取得真实正常/故障三类遥测；Holmes是否能在范围受限工具下调查。

前提盘点：现有 Colima default 已停止，4 CPU/6 GiB/30 GiB，归属不明不启动；Docker daemon不可达，无可复用运行实例。宿主16 GiB、磁盘余39 GiB、memory_pressure free36%。新建唯一专属profile `m0-otel`，4 CPU/6 GiB/24 GiB稀疏盘，禁止修改默认context，关闭SSH agent转发，仅挂载本任务源码目录。超过资源容量则报告部署失败，不停止他人服务。

版本：OTel Demo 2.0.2 / 63649d6d6a59de88fb421b88c3c3a6185b6d21ad；HolmesGPT 5e983c17f30e93099c7d775167266d4cd1d586c4。下载固定源码、保留hash；部署前解析上游公开模板和镜像digest。Compose project `opspilot-m0`，仅loopback端口，调查者不获得Docker socket或注入器目录。

顺序：1. 专属虚机与固定源码；2. 审核Compose/资源、固定镜像并启动；3. 真实流量与正常日志/指标/trace查询、身份/版本/保留映射；4. 独立只读权限拒绝测试；5. 专属受控故障与独立事实；6. 固定Holmes工具/配置准备，模型调用另等协调预算。

判据：安装/healthy容器状态不代替真实遥测；三类实际查询须有有效数据、绝对窗口、目标映射；缺测标集成失败。读权限须实际拒绝写和错误目标，未实现则不让模型使用。故障由真实服务观察确认；注入参数/答案仅开发评估者持有，不进入调查上下文。先开发案例，不宣称保留盲测、诊断普遍性、F6 Kubernetes健康或完整M0通过。

证据：docs/evidence/m0-real-environment；上游和运行数据保留在tmp/m0-environment及专属虚机，必要工件归档。失败保留命令输出并分类部署/遥测/能力问题。结束只停止自己服务，不删除数据、镜像、虚机或worktree。

## 当前进展

- 已完成固定25服务+1只读proxy容器部署、镜像digest、正常真实三类遥测查询、部署身份登记、实际HTTP拒绝及隔离网络探针；详见[环境证据](../evidence/m0-real-environment/environment-results.md)。
- 保留Prometheus首次配置失败、初始cold数据不齐、逐服务日志缺口、proxy错误分类失败及修复。原始pull日志完整保留在tmp并无损压缩归档。
- 减载关闭额外Chromium、LOCUST_USERS=2，全部服务保留；减载前后资源实测保存。
- Holmes由另一执行者维护，正常案已跑但框架无最终结论；其重试/模型预算由主Agent协调。本执行者0模型/trace。normal-03已取得上游最终业务结论，前两案失败保留；2026-09-09T17:51:53Z已仅在专属Demo注入开发故障，独立固定5m采样已取得7条真实故障trace、错误metric和HTTP500日志，已交Holmes执行故障调查；原注入脚本成功没有替代事实判据。
- 独立审查指出5m lookback和redirect缺口已修并实测；最终独立审查/故障调查/恢复与停止仍待完成。
- 必须从本worktree执行实验及停止；合并分支不迁移tmp数据。结束compose stop及colima stop m0-otel，不down，不删除容器/volume/数据。

基线逻辑提交：`2db7a9c`，Ruff/format/py_compile及git diff --check通过；故障/恢复与Holmes证据后续追加，不把基线提交当整轮完成。
