# Upgrading an OpsPilot Compose install

Schema migrations run only while every runtime is stopped (C3 §11, "部署与
schema 迁移"); there is no rolling upgrade. `install.sh upgrade` does the steps
in this order and does not rely on Compose `depends_on`:

1. stop `web`, `worker` and `observer` (all three, even if only the web was
   running);
2. start `postgres` and wait until it is healthy;
3. `migrate` (one-shot, owner login): upgrades the schema, or reports
   `unchanged` when it is already at head;
4. `init-roles` (one-shot, owner login): reuses the four runtime logins and
   checks each of them reaches the schema head;
5. start the requested runtimes: the web, plus `worker` and `observer` when
   `OPSPILOT_START_AGENT=1`. Each one verifies the schema head on start and
   exits if it is not there.

```sh
deploy/compose/install.sh upgrade
# with worker and Observer (remembered once set, see below):
OPSPILOT_START_AGENT=1 deploy/compose/install.sh upgrade
```

Secrets and settings are reused from the install's secrets directory
(`install.env` there remembers `OPSPILOT_START_AGENT`,
`OPSPILOT_OBSERVER_PROMETHEUS_URL`, the port, image and profile); no password
is regenerated, and the database volume is kept. A pulled image must be pinned
by digest (`OPSPILOT_IMAGE=<name>@sha256:<64 hex>`).

With `OPSPILOT_START_AGENT=1` the installer refuses, before any Docker call,
unless `<secrets dir>/worker_env` has a non-empty `DEEPSEEK_API_KEY=` line and
`OPSPILOT_OBSERVER_PROMETHEUS_URL` is an http(s) URL. The Observer's own
read-only Prometheus account goes in `<secrets dir>/observer_env`
(`OPSPILOT_OBSERVER_PROMETHEUS_USERNAME` / `_PASSWORD`, or `_TOKEN`).

`<secrets dir>/target_identities.json` (the web's `OPSPILOT_TARGET_IDENTITIES`)
is written once as `{}`; an install from before it gets it on upgrade. With
`{}` every Alertmanager alert is handed to a human without a Run; an entry with
`"match": {"version": 1, "labels": {...}}` lets a matching alert open a Run on
that target. With `OPSPILOT_TOOL_PROFILE=fixture` the worker knows only the
target `checkout-prod`: key the entry by that id, or the Run's tool calls are
refused (`TARGET_NOT_REGISTERED`) and it is handed to a human. The web reads
it when its container is created: after editing it run
`install.sh compose up -d --force-recreate web` (a plain `restart` keeps the
old file mount, so an editor that replaces the file leaves web on the old map).

Do not start an older image against a migrated database: the runtimes refuse
it, and there are no down-migrations. Run one web replica only; worker and
Observer also run one replica each in this install.
