# F9 #392 控制侧 Compose 一键安装：本机 Linux VM 取证（2026-10-28）

环境：colima profile `m1-kind`（Ubuntu VM，arm64，Docker 29.2.1，Compose v5.0.2），`install.sh` 在 VM 内以普通用户（uid 501）执行；Compose 项目 `opspilot-392e2e`，工作台宿主端口 18180（18080 被 kind lab 占用），密钥目录在 VM 本地 `~/.config/opspilot-392e2e`。重负载步骤包在 `lockf ~/.agent/locks/kind-lab` 内；kind lab 容器与 18080 全程未动。不是干净主机取证（那是 F9 后续步骤）。日志不含任何密钥值。

## 全新安装（`install.sh install`，密钥目录与卷都是空的）

```
18:50:49Z generated postgres_password / owner_pgpass / web_pgpass / worker_pgpass / observer_pgpass / retention_pgpass / ui_password / alertmanager_token / web_ui_users / web_event_tokens / worker_env / observer_env
18:50:49Z stopping runtimes (web worker observer) before migrating
18:50:49Z starting postgres            -> Volume opspilot-392e2e_pgdata Created, postgres Healthy
18:50:52Z migrate: start
          schema upgraded: 0023_retention_raw_cleanup
18:50:53Z migrate: done
18:50:53Z init-roles: start
          opspilot_web: login opspilot_web_app ok, schema at head 0023_retention_raw_cleanup
          opspilot_worker: login opspilot_worker_app ok, schema at head 0023_retention_raw_cleanup
          opspilot_observer: login opspilot_observer_app ok, schema at head 0023_retention_raw_cleanup
          opspilot_retention: login opspilot_retention_app ok, schema at head 0023_retention_raw_cleanup
18:50:54Z init-roles: done
18:50:54Z starting runtimes: web       -> web Healthy（web 自身 install() 校验 head 通过才会监听）
web State.StartedAt = 2026-10-28T18:50:55.085Z
```

## 安装后核查（VM 内 curl / docker inspect）

| 项 | 结果 |
|---|---|
| 生成口令登录 `GET /`（Basic，用户 `operator`） | 200；无凭据 401；错误口令 401 |
| 表单 POST 的 Origin | `http://127.0.0.1:18180` 过 Origin 检查（415，缺表单体）；`http://evil.example` 403 |
| 端口发布 | `docker port web` = `8080/tcp -> 127.0.0.1:18180`；`ss -ltn` 只有 `127.0.0.1:18180`；经 VM 地址 192.168.5.1:18180 访问超时；postgres `{"5432/tcp":null}`（不发布） |
| 容器配置中的密钥 | web、postgres 的 `docker inspect .Config` 都不含 postgres/UI/token/login 口令（leak=0）；web 环境只有 `*_FILE=/run/secrets/...` 与 `passfile=/run/secrets/web_pgpass` |
| 密钥文件 | 目录 700，13 个文件全部 600，属主为安装用户 |
| 非 root | 镜像 `Config.User = 10001:10001`；Compose 下 web 以安装用户 `501:1000` 运行 |
| 镜像内容 | `/app` 只有 `.venv`、`opspilot`；全盘找不到 `.env*`、`.git`、`docs/evidence`、`/app/tmp` |

## 生命周期

```
re-install:  schema unchanged，四个 login 复用并校验通过；secret files unchanged: yes（sha256 前后逐文件比对）；login 200
compose ps:  install.sh compose ps -> postgres healthy, web healthy
upgrade:     web Stopping/Stopped -> migrate (unchanged) -> init-roles -> web started；login 200
uninstall:   containers 0；volumes: opspilot-392e2e_pgdata；secrets kept: 13 files
re-install on kept volume: schema unchanged（不是 upgraded，数据卷沿用），login 200
uninstall --purge-data（未确认，stdin 非终端）：拒绝，"nothing removed"，卷仍在
uninstall --purge-data（OPSPILOT_PURGE_CONFIRM=项目名）：Volume opspilot-392e2e_pgdata Removed
```

## 自动化 e2e（opt-in）

`OPSPILOT_COMPOSE_E2E=1 OPSPILOT_E2E_PROJECT=opspilot-392pytest OPSPILOT_E2E_WEB_PORT=18180 python3 -m pytest --noconftest -p no:cacheprovider tests/test_392_compose_e2e_impl.py`（VM 内，pytest 取自本 worktree 的 .venv 纯 Python 包）：6 passed in 18.07s；结束后该项目 0 容器、0 卷。`init-roles` 改为按列名读行（`1566fac5`）后重跑：6 passed in 22.88s，同样无残留。

## 过程中发现并已修

- Dockerfile 首行 `# syntax=docker/dockerfile:1` 会在构建时从 Docker Hub 拉一个未固定的 frontend 镜像，本机代理断流时构建失败；删掉，用内置 frontend。
- 运维直接跑 `docker compose ps` 会因缺 `OPSPILOT_SECRETS_DIR` 等插值变量失败；新增 `install.sh compose <args>` 带上已安装设置。
- Docker Hub 拉 postgres 层多次 EOF（本机代理），重试后成功；与产品无关。
