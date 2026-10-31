# M3 运行可观测（F8 第 1–3 步）

- 状态：进行中（第 1 实施步骤合同 r1 已裁决，PR-1 产品待实施）
- 更新日期：2026-10-31
- 依据：[M3 开放决策](2026-10-27-m2-exit-m3-opening-decision.md) Q2 第 4 条、Q5、Q6；feature_list.json F8；C3 §11（可观测性、outbox、部署与 schema 迁移）；#243 #159
- 工作区：未创建

## 目标与范围

健康端点 `/healthz`、`/readyz`：只在内网监听，只报 schema 是否在 head、队列深度、最近一次模型调用状态类别；lab Prometheus 抓取，Alertmanager 两条规则（Agent 不在、模型连续失败），演练停 worker 与断模型出口各一次（第 2 步）。F8 第 1 步字段对照表（provider 状态、工具错误、卡住的 Run、token 与成本按事故和版本关联）在预审中逐项定；#243 产品模式 trace outbox 按 C3 §11 实现；结构化日志。第 3 步：带在途事故的版本升级与回滚（上一镜像 + 允许时 downgrade，否则兼容恢复）演练。不含：72 小时 soak（收口线）。

## 前提与完成条件

- 前提：交付底座线的产品镜像（升级/回滚演练需要两个固定版本）。
- 完成条件：第 1–3 步各有真实运行证据；首个实施步骤开工前完成合同预审。

## 合同（r1）

用户 2026-10-31 裁决：F8-Q1–Q4 全部按推荐 (a)，可逆默认 F8-R1–R11 照预审。完整决定表、指标名与规则原文：[F8 第 1 实施步骤合同](2026-10-27-m3-f8-operability-contract.md)。

- F8-Q1：本步 = 三进程 `/healthz` `/readyz` `/metrics` + lab Prometheus 抓取 + 两条规则 + 停 worker / 断模型出口两次演练；#243、结构化日志、#159 不在本步；F8 `passes` 保持 false。
- F8-Q2：三进程各起独立内网监听（`OPSPILOT_HEALTH_BIND`，容器内 9464），无认证、只回固定键 + 版本，不挂 web 路由表；只在 lab/开发 override 发布到宿主 127.0.0.1；跨主机接线随 F9 A4 另做预审。
- F8-Q3：自监控告警走 Alertmanager 子路由到不调用 OpsPilot 的 `opspilot-self` 接收器；交接证据 = firing 记录 + 工作台 Run 交接状态；不新增外发通知。
- F8-Q4：停 worker 不改产品；Run 停在 `queued`、`queue_depth` 上升，缩短墙钟后经页面清扫或 worker 回归得到 `DEADLINE_EXCEEDED`；自动清扫不做。
- F8-R1–R11：见合同文件。

切分：PR-1 产品（health 模块 + 三入口接线 + 模型调用计数包装器 + Compose healthcheck 与 lab override + 测试调整）→ PR-2 lab 抓取与规则 + 两次演练（PR-1 合并后）。之后各自预审：结构化日志、#243 outbox、升级回滚演练（第 3 步）。

## 下一步与交接

- 第 1 实施步骤：PR-1 产品 #413 → PR-2 lab 与演练 #414（被 #413 阻塞）。
- 结构化日志、#243 outbox、第 3 步升级回滚演练：PR-2 落地后各自预审。
