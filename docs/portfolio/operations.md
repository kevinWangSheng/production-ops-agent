# Operations

How to run a Compose install day to day. Install steps are in the [README](../../README.md#install-with-docker-compose); upgrade and rollback order is in [deploy/compose/UPGRADE.md](../../deploy/compose/UPGRADE.md). Every procedure below was exercised only in the kind lab (synthetic traffic, injected faults); the linked evidence says where and how often.

All commands run from the checkout as the installing user. `install.sh compose ...` passes its arguments to `docker compose` with the right project, files and secrets directory.

## Health and status

| Check | How | What "healthy" looks like |
|---|---|---|
| Containers | `deploy/compose/install.sh compose ps` | `web`, `worker`, `observer` running and `healthy` (each healthcheck reads its own `/readyz` on container port 9464) |
| Readiness | `/readyz` on each runtime | `200 {"status": "ready", "version": "<product version>"}`; it fails when the schema is not at head or the runtime's poll loop is stale |
| Metrics | `/metrics` on each runtime (published to the host only by the lab override `compose.lab.yaml`) | `opspilot_queue_depth`, `opspilot_model_calls_total{category}`, `opspilot_model_consecutive_failures`, `opspilot_last_poll_age_seconds`, `opspilot_schema_at_head`, `opspilot_build_info`, trace-outbox gauges |
| Logs | `deploy/compose/install.sh compose logs worker` (or `web`, `observer`) | Structured JSON lines with fixed codes and ids; no secrets |
| Work | Workbench incident list | Runs move through `queued → running → completed` or `waiting_human` with a reason |

Self-alerts: the lab ships two Prometheus rules about OpsPilot itself, `OpsPilotAgentAbsent` and `OpsPilotModelCallsFailing`, routed to a separate receiver so they never open incidents in the workbench they are about ([scripts/kind_lab/values.yaml](../../scripts/kind_lab/values.yaml)). A non-lab install has to add equivalent rules to its own Prometheus; the product does not install them.

## Common situations

| Situation | What happens on its own | What the operator does | Evidence |
|---|---|---|---|
| Worker stopped or killed | Queued Runs wait; `OpsPilotAgentAbsent` fires after 2 min; overdue Runs are parked as `DEADLINE_EXCEEDED` handoffs by the sweep. A Run cut off mid-flight resumes from its committed rows once its lease expires (up to the lease length, about 7 min in the recorded run) | Start it again: `deploy/compose/install.sh compose start worker`. The `unless-stopped` restart policy did not restart a container stopped by `docker kill` | [f8-step1-drills](../evidence/f8-step1-drills/README.md) drill 1; [f9-steps-2-4](../evidence/f9-steps-2-4-2026-11-10/README.md) step 3 |
| Model provider unreachable | Two failed calls hand the Run to a human with `MODEL_UNAVAILABLE`; `OpsPilotModelCallsFailing` fires | Restore egress or the key, then `resume` the incident in the workbench (handoffs are not retried automatically) | [f8-step1-drills](../evidence/f8-step1-drills/README.md) drill 2 |
| Need to stop the agent from querying | — | Pause globally or per target on the workbench `Controls` page. Pausing blocks new tool queries, Observer samples and model calls; un-pausing does not resume old work, each incident is resumed explicitly | F7 step 4 ([ROADMAP](../../ROADMAP.md) F7 row) |
| A Run was handed off | Incident stays open in `waiting_human` | Read the handoff reason, handle the system outside OpsPilot, register the remediation on the incident page so recovery observation can start, or ask a follow-up (opens a new Run) | F12 ([evidence](../evidence/f12-live/)) |
| Alert matched no target | Incident handed to a human, no Run | Add the target to `$SECRETS/target_identities.json`, then `install.sh compose up -d --force-recreate web` | README step 5 |
| Observer `/readyz` 503 `POLL_STALE` briefly | Happens while a slow recovery sample is in progress | None yet; tracked in [#522](https://github.com/kevinWangSheng/production-ops-agent/issues/522) | [m3-soak-2026-11-07](../evidence/m3-soak-2026-11-07/README.md) |

## Upgrade and rollback

- Upgrade: `OPSPILOT_IMAGE=ghcr.io/kevinwangsheng/opspilot@sha256:<digest> deploy/compose/install.sh upgrade` (pulled images must be pinned by digest), or `git pull && deploy/compose/install.sh upgrade` to rebuild from the checkout. The installer stops all runtimes, migrates, checks the logins and starts them again; no rolling upgrade.
- Rollback: the steps in [UPGRADE.md](../../deploy/compose/UPGRADE.md#rolling-back). When both versions have the same schema head, skip the schema-downgrade step, as UPGRADE.md now says (run anyway, it exits 1 and changes nothing). Roll back to a published digest; a locally rebuilt `opspilot:local` is not a pinned target.
- In the lab, after every `upgrade` run `python3 scripts/kind_lab.py connect-control --project opspilot` again to rejoin the runtimes to the lab network.
- Evidence: in-flight work survived image upgrade, rollback and re-upgrade on the clean host ([f9-steps-2-4](../evidence/f9-steps-2-4-2026-11-10/README.md) step 4, same schema head on both sides); a cross-schema upgrade and rollback `0023 → 0024` on a local VM ([f8-step3-upgrade-rollback](../evidence/f8-step3-upgrade-rollback/README.md)).

## Data

- Back up the database volume `<project>_pgdata` and the secrets directory together; the logins in the volume only work with the passwords in that directory. No backup tooling ships (未确认: backup and restore have not been exercised).
- Raw-payload retention: `deploy/compose/install.sh compose run --rm retention` reports what would be cleaned (raw payloads of incidents closed more than 30 days ago); add `python -m opspilot.retention --days 30 --apply` as the command to delete. The cleanup was tested against PostgreSQL in the test suite (#244); running it through the Compose service has not been exercised (未确认).
- Container logs are not rotated by the product compose file ([#476](https://github.com/kevinWangSheng/production-ops-agent/issues/476)).
- Uninstall: `deploy/compose/install.sh uninstall` keeps the volume and secrets; `--purge-data` deletes the database after you type the project name.

## Cost

Each Run's model usage is recorded per step (`opspilot_budget_reservations`, step usage). The 72h soak priced its 24 Runs at token × DeepSeek list price: CNY 4.46, not reconciled against the provider console ([soak cost](../evidence/m3-soak-2026-11-07/README.md#费用)). There is no spend cap beyond the per-Run limits.
