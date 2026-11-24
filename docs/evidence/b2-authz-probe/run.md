# B2/F7 代理探测（租机，2026-11-14）

提交 S' `748ecefa`，手册 4a.5。19:25:06Z `python3 scripts/f7_authz_probe.py > docs/evidence/b2-authz-probe/probe.json`：退出 0，`ok: true`，241/241 行与期望一致。investigator、observer、lab、holmes 四个账号的 `/api/v1/rules`、`/api/v1/targets`、`/api/v1/metadata` GET 200，HEAD 405，OPTIONS 403，holmes POST 403；`/api/v1/alerts`、`/api/v1/targets/metadata`、`/api/v1/status/config` GET 403；匿名一律 401。`sample_check`（lab 账号 GET 查询前后无新样本时间戳）通过。
