# kind 实验环境移植到 Linux 主机（F1 收尾 S4）

- 状态：进行中（实现与主机验证完成，PR 待审查与合并）
- 更新日期：2026-11-04
- 依据：F1 收尾计划（仓库外 `~/.agent/handoffs/opspilot-prereview-f1-closure-plan-2026-11-04.md`）§1 B、§2.2 S4/S5、R5；F9 第 1 步「documented automated setup」的目标侧
- 工作区：分支 `chore/linux-kind-lab`，worktree `/Users/shenghuikevin/dev/AI/poa-s4-linux-lab`

## 目标与范围

让 `scripts/kind_lab.py` 和 Holmes runner 能在 Linux 主机上跑，macOS/colima 的默认行为不变；再给出一个 Linux 主机自动安装入口。只改 `scripts/`、`tests/` 和开发文档，不碰 `opspilot/`（R5：部署脚本修复不算解冻）。

## 做了什么

- 运行方式按平台选择（`kind_lab.lab_runtime`）：macOS 用 `colima`，Linux 用 `docker`；`OPSPILOT_LAB_RUNTIME` 可覆盖。
- docker 上下文：Linux 走默认 socket；`OPSPILOT_LAB_DOCKER_CONTEXT` 可指定。macOS 仍是 `colima-m1-kind`。Holmes runner 的 `docker_env` 跟随这个设置。
- Linux 上不调 colima：`up` 用「kind 集群已存在」代替「VM 在运行」，并 `docker start` 节点容器；`stop` 只停节点容器。
- 宿主内存：Linux 读 `/proc/meminfo` 的 `MemAvailable`；Holmes runner 的 swap 记录也改读这里。
- webhook 与 OpsPilot 抓取目标：Linux 用 kind 网桥的 IPv4 网关，或 `OPSPILOT_LAB_HOST_ADDRESS`；替换后的 values 写到 `tmp/m1-kind-lab/rendered/`。macOS 仍直接传仓库文件。
- Holmes 时钟检查：Linux 直接读主机时钟。
- 窗口锁：脚本里没有调用 `lockf` 的地方，只有文档里的手工命令。Linux 用同样写法的 `flock`，写进了开发指南和 runner 的 docstring，不新增代码。
- 安装入口：`scripts/kind_lab/install_linux.sh`，只支持 Ubuntu 24.04 x86_64。安装固定版本：Docker 29.2.1、containerd 2.2.1、compose 5.1.0、buildx 0.31.1（apt hold，密钥按 SHA-256 校验），kind v0.33.0、kubectl v1.37.1、helm v4.3.0（二进制按 SHA-256 校验），版本与本机 lab 一致；另装 htpasswd、调高 inotify 上限，最后跑 `kind_lab.py up`。脚本不碰防火墙。
- 合同预审：本步不改产品行为、数据合同、权限或验收，所以没做。可逆默认由实现者决定：环境变量命名、`stop` 在 Linux 上停节点容器、values 用文本替换（地址只出现在 webhook URL 和 3 个抓取目标里）。

## 验证

- 测试先写：`tests/test_kind_lab_linux.py` 共 25 项，实现前 22 项失败。`tests/test_385_upstream_issues_impl.py` 的默认上下文测试固定用 colima 运行方式；原来它在 Linux CI 上也靠常量通过。`tests/test_414_lab_alerts_impl.py` 的 rules-check 辅助函数也固定主机地址为 colima 地址：CI 首轮在 Linux 上跑这 3 项时，因为没有 kind 网络而失败；只改了前置条件，断言没动。
- 定向测试：kind lab、holmes、#385、#399、#414、F7 授权探针相关测试共 488 项通过、5 项跳过（需要 nginx 或 docker）；修了 CI 首轮失败后，再用 `-k "kind or lab or holmes or 385 or 399 or 414 or f7"` 分别以 docker 和 colima 两种运行方式各跑一遍，都是 1400 项通过、88 项跳过。`ruff check`、`ruff format --check` 通过。`mypy` 按项目配置（`opspilot/`）通过；对 `scripts/kind_lab.py` 加跑 strict 也通过，测试文件只报出 yaml 缺类型存根，其他既有测试也是这样。本机 swap 紧张，没跑全量。
- 主机 `opspilot-f9-1`（Vultr，Ubuntu 24.04.5 x86_64，8C/15G），2026-11-04 14:22–14:39 UTC：
  - 从零开始 `git clone` 本分支，再跑 `scripts/kind_lab/install_linux.sh`（root），全程 3 分 27 秒，退出码 0。`up` 的最终 health 为 ok；运行方式 `docker`，主机地址 `172.18.0.1`。
  - 22 个 pod 都是 Running 1/1，没有重启。frontend span 速率 1.75/s。Prometheus 只读代理：investigator 账号查询 200，匿名 401，写入和 admin 都是 403。Jaeger 查到 checkout 的 3 条 trace。flagd 共 11 个开关，全部为 `off`，没有打开任何故障开关。`rules-check` 全部为 0。
  - 渲染后的 Alertmanager webhook 是 `http://172.18.0.1:8080/intake/alertmanager`，3 个抓取目标是 `172.18.0.1:1946{4,5,6}`。
  - 先 `stop`（`docker stop` 节点，退出码 137，即超时后被强杀），再 `up`：161 秒后 ok，span 速率 1.62/s，开关仍全为 off。
  - 内存：每 30 秒用 `free -m` 采样一次，共 34 次。峰值 used 2526 MiB，最低 available 13457 MiB，swap 0。`docker stats` 显示节点约 2.0 GiB（重启后 1.7 GiB）。
  - 公网监听只有 22；ufw 没动；主机上没有放模型 key 或 LangSmith key，也没有调用模型。

## 未完成与交接

- **pod 访问不到主机，webhook 会被挡住（已实测）。** 主机在 `172.18.0.1:8080` 起了临时监听：主机自己能访问（200），Alertmanager pod 访问超时，原因是 ufw 默认拒绝入站。S5 要接上工作台和健康端点，需要两步：一是让它们监听在网关地址上，二是加一条 ufw 规则，只放行 kind 网段访问 8080、19464–19466。这一条不在安装脚本里，由 lead 在 S5 裁决（改防火墙）。
- 安装脚本以 sudo 普通用户身份运行的分支（加入 docker 组后用 `sg docker` 跑 `up`）没有实测，只测了 root。R1 的 `deploy` 用户流程在 S5 验证。
- **主机已经不干净：** 上面装了 docker、kind 等工具，`/root/opspilot` 里有 lab 和凭据文件，lab 还在运行。F9 第 1 步的干净主机取证之前，要由 lead 在控制台重装系统。
- 主机进程：自己起的内存采样循环已停；lab（kind 节点容器）保持运行。
