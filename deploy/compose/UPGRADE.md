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

An older image does not start on a database a newer release has migrated:
each runtime refuses it on start. To go back to the previous release, see
[Rolling back](#rolling-back). Run one web replica only; worker and Observer
also run one replica each in this install.

`docker compose stop` gives the worker and the Observer 40 s
(`stop_grace_period`) after SIGTERM; both stop claiming new work at once. A
Run whose model or tool call is still running after that is resumed by the
next worker from its last committed step once its lease expires (up to
7 minutes). A Run started by a release whose prompt, context policy or tool
schema differs is not continued: it is blocked as `INCOMPATIBLE_STATE`, its
records stay, and a human starts a new Run from the workbench.

## Rolling back

Rolling back runs the new release's schema downgrade while every runtime is
stopped, then installs the old release over the same database and secrets
directory. The old image cannot do the downgrade itself: it does not have
the newer migrations.

1. Stop every runtime, from the new release's checkout (`postgres` keeps
   running): `deploy/compose/install.sh compose stop web worker observer`.
2. Downgrade the schema with the new release's image, the one installed
   now, to the old release's head revision (the revision its last
   `install.sh upgrade` reported as `schema upgraded:` or `schema unchanged:`,
   or the newest file in its `opspilot/migrations/versions/`):
   `deploy/compose/install.sh compose run --rm --no-deps migrate python -m opspilot.schema downgrade --to <old head>`.
   It prints `schema current:` and `schema target:` before it changes
   anything, and `schema downgraded: <old head>` when done.
3. Upgrade from the old release's checkout (or with `OPSPILOT_IMAGE` set to
   its digest): `deploy/compose/install.sh upgrade`. Its `migrate` reports
   `unchanged`, `init-roles` runs, and each runtime verifies the schema head
   on start.

A downgrade is refused, and changes nothing, when it would delete records
that only the newer schema can hold (a migration's downgrade checks this
inside its own transaction; the message names what is in the way). A
multi-revision downgrade stops at the last revision that succeeded and
prints `schema now at <revision>`. Do not restore a backup in that case:
it would erase records written since the upgrade, including human
decisions. Stay on the new release, re-run its `install.sh upgrade` to
bring the schema back to its head, and fix forward.

The `0024_trace_outbox` downgrade drops the trace outbox table, also with
rows not yet exported, and prints only how many rows were pending,
delivered and dropped.
