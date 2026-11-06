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
  `collector-restart`, `alerts` (read-only), `clock`, `ping`; anything else exits 64. Flags move
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

One rental host (`opspilot-f9-1`, shared vCPU) serves the F9 evidence and
the F1 evaluation first; the soak runs on it afterwards (results of the
evaluation archived first). The image is the `v0.1.0` tag's digest (S-R17).

1. Rental: `install_linux.sh --no-up`, then `rental_setup.sh` (prints the
   kind gateway). Before `kind_lab.py up`, put da-server's webhook token in
   the lab's token file so Alertmanager presents the token the workbench
   knows (kind_lab only creates one when the file is absent). Host to host,
   nothing printed:

   ```
   ssh da-server 'sudo -u opspilot cat /home/opspilot/.config/opspilot/alertmanager_token' \
     | ssh <rental> 'sudo -u <lab user> sh -c "umask 077; mkdir -p <repo>/tmp/m1-kind-lab; cat > <repo>/tmp/m1-kind-lab/alertmanager-webhook-token"'
   ```

   Then `OPSPILOT_LAB_HOST_ADDRESS=<gateway> OPSPILOT_LAB_HOST_PORTS=webhook=28080,web=29464,worker=29465,observer=29466 python3 scripts/kind_lab.py up`.
2. Lab credentials to da-server the same way (rental file -> ssh pipe ->
   append to the da-server file, umask 077): the Observer account into
   `observer_env` (plus `OPSPILOT_OBSERVER_PROMETHEUS_URL=http://lab-prometheus:19090`),
   the investigator account into `worker_env` (plus
   `OPSPILOT_OTEL_PROMETHEUS_URL=http://lab-prometheus:19090`,
   `OPSPILOT_OTEL_JAEGER_URL=http://lab-jaeger:16686/jaeger/ui`), the `lab`
   account into the soak's `lab.env` (`prometheus_env_file`).
3. da-server: `install.sh install` uses `compose.yaml` only
   (`deploy/compose/install.sh:282`), so after it re-create every service,
   postgres included, with the soak file set:
   `deploy/compose/install.sh compose --profile agent -f deploy/compose/compose.lab.yaml -f scripts/soak/compose.soak.yaml up -d --force-recreate`.
   `compose.soak.yaml` carries the log rotation, so a separate logging
   overlay is not needed. During the soak every compose call uses that file
   set, and `install.sh upgrade` is not run (product frozen, S-R13).
4. `python3 -m scripts.soak plan --data <dir> --t0 <ISO> --price-input <CNY/Mtok, cache miss> --price-output <CNY/Mtok> --price-source <url+date> --deployment <name> ...`
   prints the plan SHA-256; record it in the task record before T0 (S-R6).
   The plan (and T0 in it) never changes after.
5. Write `<dir>/soak.json` (below) and the operator credential file
   (`user:password`, mode 600). Smoke (P11) by hand before T0.
6. Before T0: `nohup python3 -m scripts.soak run --data <dir> &` (or a
   systemd user unit). It waits for T0 and refuses to start more than 5 min
   after it; a restart after T0 continues. Rental:
   `python3 -m scripts.soak sample --data <rental dir> --host rental --group lab`.
7. Environment-only interruptions the probe cannot see (rental reboot,
   provider maintenance): `note-interruption --data <dir> --start … --end … --cause …`;
   a lost or reinstalled rental: `note-rental-lost --data <dir> --at … --reason …`.
8. >= 30 min after the runner wrote `soak_end`:
   `python3 -m scripts.soak finish --data <dir> --merge <rental sample dir copy> --rental-hourly-usd … --rental-on … --rental-off … --balance <ts>=<CNY> --balance <ts>=<CNY>`
   (PG exports, `sizes.csv` end readings, `meta.json`, `pg_dump.sql.gz`).
9. `python3 -m scripts.soak.summarize <dir> --out summary.json`; commit
   `summary.json` + a README under `docs/evidence/m3-soak-<date>/` (S-R15).
   Raw files stay outside the repository.

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
                "soakfault@<address>"]
}
```

`env` holds non-secret values only; keys and passwords stay in files.
`prometheus_env_file` is `kind_lab.py env-file lab` output. The expected
Deployments and the token prices live in the frozen `plan.json`.

## Raw directory (the summarizer's input) and `summary.json`

The layout, every file's columns and every summary key and rule are the
module docstring of `tests/acceptance/test_soak_summarize.py` (written
independently from the contract); the code follows it. Who writes what:

| File | Writer |
|---|---|
| `plan.json` | `plan` (also carries `t0`, `schema` and each injection's `at_s` and `steps`) |
| `events.jsonl` | runner (`soak_start`/`soak_end`, `injection_on`/`_off`/`_skipped`, `tunnel_down`/`_up`, `env_interruption_*` for tunnel outages outside A4, `env_action`, `clock_check`, plus `step`, `operator`, `open_incidents`, `error`, `sizes`, `export`), `note-interruption`, `note-rental-lost` |
| `readyz.csv`, `prom_up.csv`, `ksm.csv`, `alerts.jsonl` | runner (30 s / 30 s / 60 s / 60 s); alerts come from the lab Alertmanager API over the fault key (`fault.sh alerts`, read-only) |
| `docker_stats.csv`, `host_stats.csv` | runner on da-server (group `shared` for non-OpsPilot containers), `sample` on the rental (group `lab`), merged by `finish --merge` |
| `sizes.csv` | runner at start, `finish` at the end (`pg_database`, `docker_*` from `docker system df`) |
| `deliveries.jsonl`, `runs.jsonl`, `sessions.jsonl`, `samples.csv`, `meta.json` | `finish` |

Mappings `finish` makes: firing deliveries only; intake outcome
`created`/`replayed` -> `run`, `handoff_created`/`handoff_replayed` -> `handoff`,
`attached` -> `attached`; `outcome_at` = `received_at` (the decision is taken
in the receiving transaction). Runs: `created_at` = the alert identity's
creation for an incident's first Run, else the runner's first sight;
`started_at` = first sight as `running` (or the first step); `finished_at` =
first sight in a terminal state; `wall_clock_limit_s` = deadline - start.
Sessions: `started_at` = authorization, `max_duration_s` = deadline -
authorization. Samples: adopted samples only; `stale` -> `stale`,
`no_data`/`timeout`/`failed` or a required signal absent -> `missing`.
Runner-only files: `run_states.jsonl`, `frontend_rate.jsonl`, `prom/*.json`.

## What the evidence README must state

- Incident closes are scripted on behalf of a human (S-R3), with the B1
  extension: shortly before the next injection (10 min), with no fault on
  and no observation in flight, an `open` incident whose current Run is
  `waiting_human`/`failed`/`blocked`/`budget_exhausted` is closed too
  (reason `before_next_injection_run_<state>`). `summary.json` `operator`
  lists closes by reason, injections that attached to an earlier incident,
  and the incidents still open when each injection started.
- A restoring step (flag off, egress restore, tunnel start, worker up) that
  fails is retried every 60 s until it succeeds; new flag injections wait
  meanwhile. `stuck_work.injections_left_on` lists any injection left on.
- Noise toggles must not open a Run (U2): `injections[].opened_work` (noise
  only; `null` for other kinds and unexecuted noise) lists every delivery
  with outcome `run`/`handoff` received between the toggle's `injection_on`
  and its `injection_off` + 600 s (no off: the soak's end), naming the
  `episode` when it is also an episode's linked delivery;
  `stuck_work.noise_opened_work` has their delivery ids. Reported, not a
  step-4 code.
- `prom_up.csv` rows with `source=query_failed` are failed queries
  (Prometheus or tunnel), counted as `up=0` for all three roles.
- One shared-vCPU rental host; not production evidence.

## Limits

- Token cost is a lower bound: only committed steps carry `usage`.
- Run times not stored in PG (created for a later Run, started, finished)
  have the runner's 60 s resolution.
- The rental host has shared vCPUs: resource and latency numbers describe
  this run only and are not comparable.
- KSM series can outlive their pod; pod presence is not proof of service.
- Prometheus on the rental keeps its TSDB in the kind node; the 6-hourly
  exports are the record (S-R15).
