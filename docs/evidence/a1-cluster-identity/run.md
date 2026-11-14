# A1 只读集群身份：拒绝取证（租机，2026-11-14）

类别：集成运行（真实 kind 集群），不调模型。租机见 [租机记录](../rental-2026-11/run.md)；手册阶段 4a。提交 S' `748ecefa`。时间 UTC。

- RBAC 清单 `scripts/kind_lab/opspilot-readonly-rbac.yaml` sha256 `ddd4299d538ecfeb48ed272bd2f7d6306c54778f8c188ae09366a4b4370fdea0`（与 `k8s-token.json` 记录一致）。
- token：SA `opspilot-readonly/opspilot-investigator`，请求 24h，签发 2026-11-14T16:34:06Z，过期 2026-11-15T16:34:06Z（`expires_at_source: token_request`；没有被截短，A1 风险 2）。
- 已知秘密值文件 10 行（6 个 lab Prometheus 口令、Alertmanager webhook token、release-deployer token、DeepSeek 与 LangSmith key），仓库外 600。

| 侧 | 时刻 | 命令 | 结果 |
|---|---|---|---|
| 宿主 | 19:20:04 | `k8s_authz_probe.py --env-file tmp/m1-kind-lab/k8s-investigator.env --known-secrets … --side host --manifest … --out-dir docs/evidence/a1-cluster-identity/host` | 退出 0，`ok: true`；37/37 行与期望一致：读类 200，Secret/watch/exec/attach/port-forward/proxy/token/impersonate/写类 dry-run 403，5 类自定义资源 403；ConfigMap 扫描 20 个、命中 0 |
| worker 容器 | 19:24:59 | `docker exec -i <worker> python -c "$(cat scripts/k8s_authz_probe.py)" --env-file /run/secrets/worker_env --side worker --known-secrets -` | 退出 0，`ok: true`，`api_url https://opspilot-m1-control-plane:6443` 能连上（证书名对得上，A1 风险 1），37/37 一致，命中 0 |

- Compose（只为容器侧探测）：19:20:53 `control-secrets` → `OPSPILOT_START_AGENT=1 deploy/compose/install.sh install`（退出 0，34 秒）→ `connect-control --project opspilot` → 再一次 `control-secrets --project opspilot`（重建 worker 并挂 CA），四个容器 healthy；19:28:09 `install.sh uninstall`（保留数据卷与密钥）。容器过滤标签 `com.docker.compose.project=opspilot`、`com.docker.compose.service=worker` 可用。
- token 原文搜索 `docs/evidence/`：0 命中（grep 退出 1）；已知秘密值逐个搜索：0 命中。
