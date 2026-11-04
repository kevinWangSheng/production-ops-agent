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
- webhook 与 OpsPilot 抓取目标：Linux 上改为控制侧容器在 kind 网络上的别名（`opspilot-web:8080`、`opspilot-{web,worker,observer}:9464`），由新子命令 `connect-control --project <p>` 用 `docker network connect --alias` 接入。设置 `OPSPILOT_LAB_HOST_ADDRESS` 时退回主机地址加主机端口。替换后的 values 写到 `tmp/m1-kind-lab/rendered/`；macOS 仍直接传仓库文件。
- Holmes 时钟检查：Linux 直接读主机时钟。
- 窗口锁：脚本里没有调用 `lockf` 的地方，只有文档里的手工命令。Linux 用同样写法的 `flock`，写进了开发指南和 runner 的 docstring，不新增代码。
- 安装入口：`scripts/kind_lab/install_linux.sh`，只支持 Ubuntu 24.04 x86_64。安装固定版本：Docker 29.2.1、containerd 2.2.1、compose 5.1.0、buildx 0.31.1（apt hold，密钥按 SHA-256 校验），kind v0.33.0、kubectl v1.37.1、helm v4.3.0（二进制按 SHA-256 校验），版本与本机 lab 一致；另装 htpasswd、调高 inotify 上限，最后跑 `kind_lab.py up`。脚本不碰防火墙。
- 合同预审：本步不改产品行为、数据合同、权限或验收，所以没做。可逆默认由实现者决定：环境变量命名；`stop` 在 Linux 上只停节点容器；values 用文本替换（地址只出现在 webhook URL 和 3 个抓取目标里，注释不改）；pod 到控制侧走 kind 网络。最后一条由 lead 2026-11-04 定为可逆接线细节，理由是不用改控制侧「只绑 127.0.0.1」，也不用改 ufw，与 #393 本机证据接法相同（`docs/tasks/2026-10-27-m3-f9-delivery.md:69`）。主机网关加 ufw 只作退路。

## 验证

- 测试先写：`tests/test_kind_lab_linux.py` 第一版共 25 项，实现前 22 项失败。改成 kind 网络接法后共 26 项，新测试放到上一版实现上跑，7 项失败。`tests/test_385_upstream_issues_impl.py` 的默认上下文测试固定用 colima 运行方式；原来它在 Linux CI 上也靠常量通过。`tests/test_414_lab_alerts_impl.py` 的 rules-check 辅助函数也固定主机地址为 colima 地址：CI 首轮在 Linux 上跑这 3 项时，因为没有 kind 网络而失败；只改了前置条件，断言没动。
- 定向测试：kind lab、holmes、#385、#399、#414、F7 授权探针相关测试共 488 项通过、5 项跳过（需要 nginx 或 docker）；修了 CI 首轮失败后，再用 `-k "kind or lab or holmes or 385 or 399 or 414 or f7"` 分别以 docker 和 colima 两种运行方式各跑一遍，都是 1400 项通过、88 项跳过。`ruff check`、`ruff format --check` 通过。`mypy` 按项目配置（`opspilot/`）通过；对 `scripts/kind_lab.py` 加跑 strict 也通过，测试文件只报出 yaml 缺类型存根，其他既有测试也是这样。本机 swap 紧张，没跑全量。
- 主机 `opspilot-f9-1`（Vultr，Ubuntu 24.04.5 x86_64，8C/15G），2026-11-04 14:22–14:39 UTC：
  - 从零开始 `git clone` 本分支，再跑 `scripts/kind_lab/install_linux.sh`（root），全程 3 分 27 秒，退出码 0。`up` 的最终 health 为 ok；运行方式 `docker`。这一轮用的还是旧版网关地址，下面第三条记的 webhook 和抓取目标已被 kind 网络接法取代。
  - 22 个 pod 都是 Running 1/1，没有重启。frontend span 速率 1.75/s。Prometheus 只读代理：investigator 账号查询 200，匿名 401，写入和 admin 都是 403。Jaeger 查到 checkout 的 3 条 trace。flagd 共 11 个开关，全部为 `off`，没有打开任何故障开关。`rules-check` 全部为 0。
  - 渲染后的 Alertmanager webhook 是 `http://172.18.0.1:8080/intake/alertmanager`，3 个抓取目标是 `172.18.0.1:1946{4,5,6}`。
  - 先 `stop`（`docker stop` 节点，退出码 137，即超时后被强杀），再 `up`：161 秒后 ok，span 速率 1.62/s，开关仍全为 off。
  - 内存：每 30 秒用 `free -m` 采样一次，共 34 次。峰值 used 2526 MiB，最低 available 13457 MiB，swap 0。`docker stats` 显示节点约 2.0 GiB（重启后 1.7 GiB）。
  - 公网监听只有 22；ufw 没动；主机上没有放模型 key 或 LangSmith key，也没有调用模型。

- 独立审查：Codex `gpt-6-astra`，全新上下文，会话 `01a1a741…`，只拿到目标、预审 §1 B、本记录和 PR diff。发现 1 项阻塞：Linux 默认路径没有清掉 shell 里继承的 `DOCKER_CONTEXT`，lab 仍会连到那个上下文。处理方法是先补回归测试（修复前 1 项失败），再在 `kind_lab.docker_env` 和 Holmes 的 `docker_env` 里先去掉继承值。复验通过：Linux 走默认 socket；`OPSPILOT_LAB_DOCKER_CONTEXT`、`F14_DOCKER_CONTEXT` 和 macOS 的 colima 上下文照常生效；没有引入新缺口。其余要求逐项核对，都已满足。
- 收尾四个数：实施期间新冒出的合同问题 0；因合同变更重写的测试 0；环境重跑次数 2（主机上一次 stop/up 循环、一次 kind 网络接法的 `up`）；用户裁决轮数 0（接线方式由 lead 定为可逆细节）。

## 未完成与交接

- pod 到控制侧的连通已解决，2026-11-04 15:48 UTC 在主机上实测。先确认旧做法确实不通：主机网关加临时监听时，主机访问得到 200，Alertmanager pod 访问超时，原因是 ufw 默认拒绝入站。改为 kind 网络之后：先 `up` 重新渲染，再起 3 个带 Compose 标签的临时探针容器（项目 `s4probe`，不发布端口），用 `connect-control --project s4probe` 接入（再次运行显示 already connected，项目不存在时退出码 1）。随后 pod 访问 `opspilot-web:9464` 得到 200；用 `amtool alert add` 发一条测试告警，探针记到 `POST /intake/alertmanager`，带 Bearer、683 字节；Prometheus `up{job="opspilot"}` 三个目标都是 1。探针容器、网络和测试告警都已清掉（告警 3 分钟后过期），ufw 没动。
- 未测：安装脚本以 sudo 普通用户身份运行的分支（加入 docker 组后用 `sg docker` 跑 `up`）没有实测，只测了 root。R1 的 `deploy` 用户流程在 S5 验证。
- Linux 上 `docker stop` 节点时退出码 137（10 秒超时后被强杀）；之后 `up` 照常恢复，不影响使用。
- 待办：lead 重装主机后，在干净主机上只跑一次文档化安装，作为 F9 第 1 步目标侧证据。
- **主机已经不干净：** 上面装了 docker、kind 等工具，`/root/opspilot` 里有 lab 和凭据文件，lab 还在运行。F9 第 1 步的干净主机取证之前，要由 lead 在控制台重装系统。
- 主机进程：自己起的内存采样循环已停；lab（kind 节点容器）保持运行。
