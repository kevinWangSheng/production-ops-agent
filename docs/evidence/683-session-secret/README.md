# #683 固定 web 会话密钥：本地 Compose 实测

日期 2026-11-22（本机 colima），分支 `feat/683-session-secret`。按 B3-U2（a）、B3-R12、B3-R13。
这是本地部署实测，不是生产观察；不涉及调查循环，没有真实 Run。记录里不含任何密钥值。

## 环境

```sh
export OPSPILOT_PROJECT=opspilot-683 OPSPILOT_WEB_HOST_PORT=18683
export OPSPILOT_SECRETS_DIR=$HOME/.agent/tmp/opspilot-683/secrets   # 一次性目录，实测后已删除
```

登录用这次一次性安装生成的 `operator` 用户（密码读自 `$OPSPILOT_SECRETS_DIR/ui_password`，curl `--data-urlencode password@file`，不出现在命令行或输出里）。

## 步骤与结果

| # | 操作 | 结果 |
|---|---|---|
| 1 | `deploy/compose/install.sh install` | exit 0；日志 `generated web_session_secret`；文件 `-rw-------`，64 字节；schema `0028_runbook_imports` |
| 2 | `POST /login`（Origin 头 = 本机 workbench）存 cookie A；`GET /`（Accept: text/html）带 A | 303；200 |
| 3 | 对照：`GET /` 不带 cookie | 302 → `/login?next=%2F` |
| 4 | `deploy/compose/install.sh compose restart web` | 容器 `StartedAt` 由 19:42:48Z 变为 19:43:34Z（确实重启） |
| 5 | `GET /` 带 A | **200**，没有跳登录页 |
| 6 | web 容器 PID 1 环境：`OPSPILOT_SESSION_SECRET_FILE` 0 条，`OPSPILOT_SESSION_SECRET` 1 条；web 日志里「OPSPILOT_SESSION_SECRET not set」0 条 | entrypoint 的 `_FILE` 读入生效 |
| 7 | 模拟老安装：删除 `web_session_secret`（数据库卷仍在），`deploy/compose/install.sh upgrade` | exit 0，没有被「已有数据库必须存在」检查拒绝；日志 `generated web_session_secret`；`schema unchanged`；init-roles 四个登录全部 ok；新文件 `-rw-------`，64 字节 |
| 8 | `GET /` 带 A（旧密钥签的 cookie） | 302 → 登录页：第一次补生成会退出一次登录，与 UPGRADE.md 写的一致；也说明 web 用的是新文件（轮换路径成立） |
| 9 | 重新登录存 cookie B；再跑一次 `upgrade` | 登录 303；upgrade exit 0，日志里没有再生成密钥 |
| 10 | `GET /` 带 B | **200**：登录跨 upgrade 保持 |
| 11 | 在 install/upgrade 日志、web 容器日志、`docker inspect` 输出里搜密钥值 | 0 处命中 |

第 4 步须用 `install.sh compose restart web`：直接 `docker compose restart web` 因缺 `OPSPILOT_RUNTIME_UID` 等安装器导出的变量而在插值阶段失败（这是现有行为，与本改动无关）。

## 定向测试

```sh
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest \
  tests/acceptance/test_392_compose_contract.py tests/acceptance/test_392_install_contract.py -q
# 10 passed
bash -n deploy/compose/install.sh && sh -n deploy/docker-entrypoint.sh   # ok
```

## 清理

`OPSPILOT_PURGE_CONFIRM=opspilot-683 deploy/compose/install.sh uninstall --purge-data`（只删本项目的容器、网络与数据库卷），删除一次性 secrets 目录和 `~/.local/state/opspilot/installs/opspilot-683` 记录。`opspilot:local` 镜像保留。
