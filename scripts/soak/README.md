# 72h lab soak scripts (F8 step 4)

Contract: the soak prereview S-R1–S-R18 and preconditions P1–P14, recorded in
[the task record](../../docs/tasks/2026-11-04-m3-soak.md). Lab tooling only:
nothing here is imported by `opspilot/`, the product image does not contain
it, and the product's permissions do not change. The only product write the
soak makes is the workbench `close` action over HTTP, scripted on behalf of a
human (S-R3) and labelled so in every record.

All Python runs from the repository root as `python3 -m scripts.soak …`
(standard library only, Python ≥ 3.10).

## Topology (S-R1, S-R2)

```
rental host (kind lab)                         da-server (control side, Compose project "opspilot")
  Prometheus proxy 127.0.0.1:19090  <-- -L --  soak-tunnel (autossh) = lab-prometheus:19090, 127.0.0.1:19090
  Jaeger           127.0.0.1:16686  <-- -L --                        = lab-jaeger:16686
  kind gateway 172.18.0.1:28080     -- -R -->  web:8080   (Alertmanager webhook)
               172.18.0.1:29464-6   -- -R -->  web/worker/observer:9464 (scrape targets)
  /opt/opspilot-soak/fault.sh  <-- ssh soakfault (forced command) --  runner
```

- `rental_setup.sh` (rental, root, once after `install_linux.sh`): accounts
  `soaktunnel` (forwards only, `permitopen`/`permitlisten` pinned) and
  `soakfault` (forced command `sudo -n -u <lab user> /opt/opspilot-soak/fault.sh`),
  an sshd drop-in (`GatewayPorts clientspecified` for `soaktunnel` only) and, if
  ufw is active, rules admitting the kind subnet on the kind bridge to the four
  gateway ports only. It prints the two lab variables; then run
  `OPSPILOT_LAB_HOST_ADDRESS=172.18.0.1 OPSPILOT_LAB_HOST_PORTS=webhook=28080,web=29464,worker=29465,observer=29466 python3 scripts/kind_lab.py up`
  so the lab's webhook and scrape targets point at the tunnel.
- `fault.sh`: accepts exactly `flag <paymentFailure|adManualGc|loadGeneratorFloodHomepage> <on|off> <item>`,
  `collector-restart`, `clock`, `ping`; anything else exits 64. Flags move
  through `lab_fault.py` (idempotent, verified against the live ConfigMap).
- `compose.soak.yaml` (da-server): the `soak-tunnel` sidecar (image built from
  `tunnel/Dockerfile` with the operator's uid) and json-file log rotation
  100 MB × 10 on every service (#476). The worker's and the Observer's lab
  URLs are `http://lab-prometheus:19090` and `http://lab-jaeger:16686/jaeger/ui`.
  Every compose call during the soak goes through
  `deploy/compose/install.sh compose --profile agent -f deploy/compose/compose.lab.yaml -f scripts/soak/compose.soak.yaml`;
  never `install.sh upgrade` during the soak (the product is frozen, S-R13, and
  that path would drop the overlay).

## Procedure

1. Preconditions P1–P13 green (task record). Data directory on da-server, e.g.
   `/home/opspilot/soak-2026-11-xx/` (later copied to `~/.agent/soak-<date>/`).
2. `python3 -m scripts.soak plan --data <dir>` → prints the plan SHA-256; put it
   in the task record before T0 (S-R6). The plan never changes after.
3. Write `<dir>/soak.json` (below), the operator credential file (`user:password`,
   mode 600) and, optionally, `<dir>/cost.json`.
4. Smoke (P11) by hand, then start: `nohup python3 -m scripts.soak run --data <dir> &`
   (or a systemd user unit). T0 is its first tick. The runner is restart-safe.
5. Rental host: `python3 -m scripts.soak sample --data <rental dir> --host rental`
   (resources every 60 s), copied next to the da-server files at the end.
6. Environment-only interruption the probe cannot see (rental reboot,
   provider maintenance): `python3 -m scripts.soak note-interruption --data <dir> --start … --end … --reason …`.
7. ≥ 30 min after the runner ends: `python3 -m scripts.soak finish --data <dir>`
   (PG export `pg.json`, `pg_dump.sql.gz`, sizes).
8. `python3 -m scripts.soak summarize --data <dir> --out summary.json`; commit
   `summary.json` + README under `docs/evidence/m3-soak-<date>/` (S-R15). Raw
   files stay outside the repository.

## `soak.json`

```json
{
  "compose": ["deploy/compose/install.sh", "compose", "--profile", "agent",
              "-f", "deploy/compose/compose.lab.yaml", "-f", "scripts/soak/compose.soak.yaml"],
  "compose_egress": ["-f", "deploy/compose/compose.drill-egress.yaml"],
  "env": {"SOAK_RENTAL_HOST": "<address>", "SOAK_SECRETS_DIR": "/home/opspilot/.soak-secrets",
          "SOAK_LOG_DIR": "/home/opspilot/soak-<date>", "SOAK_KIND_GATEWAY": "172.18.0.1"},
  "workbench": "http://127.0.0.1:18080",
  "operator_credentials_file": "/home/opspilot/.soak-secrets/operator",
  "health": {"web": "http://127.0.0.1:19464/readyz", "worker": "http://127.0.0.1:19465/readyz",
             "observer": "http://127.0.0.1:19466/readyz"},
  "prometheus": "http://127.0.0.1:19090",
  "prometheus_env_file": "/home/opspilot/.soak-secrets/lab.env",
  "fault_ssh": ["ssh", "-i", "/home/opspilot/.soak-secrets/soak_fault_key", "-o", "BatchMode=yes",
                "-o", "UserKnownHostsFile=/home/opspilot/.soak-secrets/soak_known_hosts",
                "soakfault@<address>"],
  "expected_deployments": []
}
```

`env` holds non-secret values only; keys and passwords stay in files.
`prometheus_env_file` is `kind_lab.py env-file lab` output. An empty
`expected_deployments` means "the Deployments seen in the first lab sample".

## Data files (inputs of `summarize`)

All timestamps are ISO 8601 UTC with `Z`. JSON-lines files tolerate a torn
last line.

| File | Writer | One record |
|---|---|---|
| `plan.json` | `plan` | `{schema, duration_s, items:[{id, kind: episode\|noise\|agent_fault, at_s, duration_s?, flag?, steps:[{id, action, at_s \| after+delay_s, condition?, max_wait_s?, flag?}]}]}` |
| `events.jsonl` | runner, `note-interruption`, `finish` | `{at, kind, …}`; kinds: `start {t0, plan_sha256}`, `step {step, action, status: waiting\|retry\|ok\|failed\|skipped, due, started, exit, output, stderr, reason?, condition?, condition_met?}`, `operator {action: close, actor, scripted: true, incident_id, expected_generation, reason, status, error, response}`, `open_incidents {before_step, incidents}`, `env {action, …}`, `sizes {phase, db_bytes, docker_df}`, `export {path}`, `interruption {start, end, reason}`, `error {loop, error}`, `end {t0, interruption_s}`, `finish` |
| `probe.jsonl` | runner, every 30 s | `{at, target: web\|worker\|observer\|tunnel, status: int\|null, error, opspilot_up?: {instance: "1"\|"0"} \| null}` |
| `runs.jsonl` | runner, every 60 s, on change | `{at, run_id, state}` |
| `lab.jsonl` | runner, every 60 s | `{at, deployments: {name: pods} \| null, frontend_span_rate: float \| null}` |
| `resources-<host>.jsonl` | runner / `sample`, 60 s | `{at, host, mem_available_kib, swap_free_kib, load1, disk_free_bytes, docker_exit, containers:[{name, cpu_pct, mem_bytes}]}` |
| `clock.jsonl` | runner, start / 6 h / end | `{at, local_ntp_synchronized, local_chrony, remote_exit, remote_output, rtt_s, offset_s}` |
| `prom/<end>.json` | runner, every 6 h and at the end | `{start, end, step_s: 60, series: {opspilot_up\|alerts\|frontend_span_rate\|checkout_error_ratio: {expr, status, error, result}}}` (`result` = Prometheus matrix) |
| `pg.json` | `finish` | `{exported_at, t0, incidents, runs, deliveries, sessions, endings, controls, postmortem_tokens, database}` (columns in `pg.export_queries`) |
| `cost.json` | engineer | `{deepseek_cny_per_mtok: {input_cache_hit, input_cache_miss, output}, price_source, balance_cny: {start, end}, rental: {usd_per_hour, started_at, ended_at, source}}` |
| `soak.json` | engineer | config above |

## `summary.json`

`window` (S-R4), `faults` (S-R6: each item executed, or not executed with
each step's recorded reason; a step with no record is `documented: false`),
`availability` (S-R7: readyz per role, tunnel, `up{job="opspilot"}` per role
with tunnel-down and readyz-ok-but-scrape-down counts, end-to-end per
episode), `missing_observations` (S-R8: recovery samples expected vs adopted,
scrape gaps ≥ 2 steps, KSM pod presence), `stuck_work` (S-R9), `resources`
(S-R10, OpsPilot containers vs the rest), `cost` (S-R11), `operator`,
`clock` (S-R5), `inputs` (SHA-256 of every file read) and `step4` (S-R12:
`achieved` = coverage, faults accounted for, the five sections published,
every injected episode has a destination; `passes_flipped` is always false).

## Limits

- Token cost is a lower bound: only committed steps carry `usage`.
- `queued > 10 min` has the runner's 60 s resolution; scrape gaps the export's
  60 s resolution.
- KSM series can outlive their pod; pod presence is not proof of service.
- Prometheus on the rental keeps its TSDB in the kind node; the 6-hourly
  exports are the record (S-R15).
