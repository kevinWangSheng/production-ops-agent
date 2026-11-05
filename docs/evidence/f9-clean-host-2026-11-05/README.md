# F9 第 1 步目标侧：干净主机一键安装 kind lab（2026-11-05）

- 结论：**通过**。在刚重装的主机上，从 `git clone` main 开始，跑一条文档化命令 `scripts/kind_lab/install_linux.sh`（root），退出码 0，耗时 3 分 06 秒，lab 就绪（`kind_lab.py health` ok）。
- 主机：Vultr 租机，Ubuntu 24.04.5 LTS（内核 6.8.0-147，x86_64，8 vCPU，15.6 GiB 内存），控制台 Reinstall 后首次登录约 1 分钟。公网 IP 在日志里替换成了 `<rental-host>`。
- 代码：main `b8ba67c65b2439764d10ce259833b9ea42bde350`。安装入口与 kind lab 脚本来自 #462（Linux 移植）和 #479（多实例）。
- 时间（UTC）：取干净状态 08:08:01，开始安装 08:10:14，安装结束 08:13:20，安装后检查 08:14:29。对应太平洋时间 11-05 00:08–00:14。
- 范围：只是「搭好所选真实软件环境」这一侧。F9 第 1 步里「接上 Agent」的控制侧安装（`deploy/compose/install.sh`）不在这里。

## 文件

| 文件 | 内容 |
|---|---|
| `00-clean-state.txt` | 改动前的状态：`uptime` 1 min、`dpkg -l` 832 行（827 个 ii）、PATH 上没有 docker/kind/kubectl/helm、`/usr/local/bin` 为空、没有 `opseval` 用户、ufw 只放行 22、只监听 22 和本地 DNS。镜像自带 `/usr/bin/htpasswd` |
| `01-sshd.txt` | sshd 加固：`00-opspilot.conf` 关闭口令登录，root 只允许密钥；`sshd -t` 通过，`sshd -T` 生效；口令方式登录被拒 |
| `02-opseval.txt` | 评测用户 `opseval`：口令锁定，没有 sudo（docker 组在装好 docker 之后才加） |
| `03-start-commit.txt` | 开始时间与 clone 下来的 commit |
| `03-install.log` | 安装命令的完整输出，含 `/usr/bin/time -v` 统计 |
| `04-post-install.txt` | 安装后检查，见下文 |

## 安装后检查（`04-post-install.txt`）

- 固定版本：docker 29.2.1，kind v0.33.0，kubectl v1.37.1，helm v4.3.0。docker 的 5 个包已 `apt-mark hold`。
- 22 个 pod 全部 Running 1/1，没有重启。frontend span 速率 1.57/s。
- Prometheus 只读代理：investigator 账号查询 200，匿名 401，写入 403。Jaeger 查到 checkout 的 3 条 trace。
- flagd 共 11 个开关，全部为 `off`。`kind_lab.py rules-check` 4 项都为 0。
- 公网只监听 22；ufw 没动。
- 内存：安装期间每 15 s 采样一次 `free -m`，峰值 used 2356 MiB，最低 available 13627 MiB，swap 0。`docker stats` 显示节点 1.95 GiB。

## 之后在同一台主机上做的（不属于干净安装本身）

- `opseval` 从自己的 checkout 起了评测用的实例 0/1/2。实例 0 复用了 root 安装时建的集群，同时接手了它的凭据。凭据换了，但 otel-collector 没有滚动，span 指标因此中断；手工 `rollout restart deployment/otel-collector` 后恢复。见 #488，修复因评测冻结暂缓。
- 安装了 PostgreSQL 17 二进制（PGDG 源，不建默认集群）和 uv 0.10.8，供评测 harness 使用；它们不在安装脚本里。
- 主机上没有放任何模型 key 或 LangSmith key，也没有调用模型。日志里只有密钥的名称和文件路径，没有任何值。
