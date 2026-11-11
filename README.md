# OpsPilot — Read-only Operations Investigator

English | [简体中文](README.zh-CN.md)

OpsPilot is a continuously running agent for incident and post-release investigation. It reads telemetry and never executes production changes or remediation. Recovery observation (read-only) can be authorized automatically after a qualifying conclusion; everything else stays under human control.

Internal process documents are written in Chinese. Where this README and the Chinese contract documents ([SPEC.md](SPEC.md), [PRODUCT-CONSTRAINTS.md](PRODUCT-CONSTRAINTS.md), the [C3 technical design](docs/design/technical-proposal-2026-09-07.md), task records) disagree, the Chinese contract documents are authoritative.

## What it does

- **Investigates.** An Alertmanager alert opens an incident. When the alert matches a configured target, an investigation Run starts; otherwise the incident is handed to a human. The model gathers evidence through read-only, target-scoped tools, keeps competing hypotheses, and publishes a report that separates facts, hypotheses and gaps. When evidence is missing it says so, or hands the incident to a human, instead of guessing.
- **Works with a human.** In the workbench an operator reads the report, asks follow-up questions (each opens a new Run), pauses investigation globally or per target, and closes or reopens incidents. Human control always takes precedence over the agent.
- **Observes recovery independently.** After a human has handled an incident, or automatically after a qualifying conclusion, a separate Observer process with its own read-only Prometheus account samples service health and records whether the service recovered. It never performs a recovery action.
- **Drafts reviewed postmortems.** The worker drafts a postmortem from the incident record; it becomes knowledge for later investigations only after a human approves it in the workbench.
- **Watches releases.** A version-change event starts an observation window; a delayed regression becomes an incident. OpsPilot never approves, blocks or executes a release.

What it will not do is fixed in [PRODUCT-CONSTRAINTS.md](PRODUCT-CONSTRAINTS.md): no production writes, no release gates, no execution of recovery steps.

## Status

Milestone M3 (effect and delivery acceptance) is open: most of its evidence is in, and F1 and F9 are not yet marked as passing. This is a portfolio project built to production engineering standards; it has **never run in production**.

- 9 of 11 features pass all their acceptance steps in [feature_list.json](feature_list.json) (F2, F3, F6, F7, F8, F11, F12, F13, F14). F1 and F9 are open. F8 (operability) passed on lab evidence only, not production.
- Everything was measured in a kind lab running the OpenTelemetry Demo: synthetic load-generator traffic, injected faults, a real model (DeepSeek). That includes a [same-condition comparison with HolmesGPT](docs/results/f1-same-condition-comparison-2026-11.md) (F1 step 5, calibrated judging, is not achieved), a [72-hour lab soak](docs/evidence/m3-soak-2026-11-07/README.md), and an install, full incident flow, worker kill and pinned-image upgrade/rollback on a freshly reinstalled host ([step 1](docs/evidence/f9-clean-install-2026-11-09/README.md), [steps 2–4](docs/evidence/f9-steps-2-4-2026-11-10/README.md), several items only partially passed).
- Releases are tagged and published to GHCR by the release workflow. The latest is `v0.1.1` (2026-11-10); the soak and the clean-host runs used `v0.1.0`, `ghcr.io/kevinwangsheng/opspilot@sha256:228df9f72e6964e1d7d7110365d3b59330d7fc93ebd89e62b70631de6c7aaabd`.

For a guided tour, start at the [portfolio walkthrough](docs/portfolio/README.md); what the evidence does not show is in [limitations](docs/portfolio/limitations.md). Work order: [ROADMAP.md](ROADMAP.md).

## Install with Docker Compose

The install runs on the control side: one Linux host running PostgreSQL, the web workbench, the worker (investigator) and the Observer, each as its own container from one product image. The workbench is published on `127.0.0.1` only; PostgreSQL publishes no port.

You need a Linux host with Docker Engine and the Compose v2 plugin, `bash`, `python3` (standard library only, used to hash the generated credentials) and this repository checked out. Run everything as a dedicated **non-root** user that can use Docker; `install.sh` refuses to run as root. macOS with colima works for development only.

On an image that only allows root logins, create that user as root and continue from its login shell; it needs no sudo (to run the pinned lab on the same host, follow [the lab section](#the-opentelemetry-demo-lab-on-the-same-host) instead, which also installs Docker):

```sh
adduser --disabled-password --gecos "" opspilot
usermod -aG docker opspilot
su - opspilot
```

### 1. Install

```sh
git clone https://github.com/kevinWangSheng/production-ops-agent.git
cd production-ops-agent
deploy/compose/install.sh install
```

This builds the image `opspilot:local` from the checkout, generates every password and token once, migrates the database, creates the four runtime database logins and starts the web workbench. Secrets go to `${XDG_CONFIG_HOME:-$HOME/.config}/opspilot` (outside the repository, mode 0600; set `OPSPILOT_SECRETS_DIR` to choose another directory). The steps below refer to that directory as `$SECRETS` and to the workbench port as `$PORT`:

```sh
SECRETS="${OPSPILOT_SECRETS_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/opspilot}"
PORT="${OPSPILOT_WEB_HOST_PORT:-18080}"
```

If you installed with a custom `OPSPILOT_SECRETS_DIR`, export it again in this shell, or set `SECRETS` to the directory `install.sh install` printed (its last lines say where `ui_password` and the Alertmanager token are). Likewise, if you installed with a custom `OPSPILOT_WEB_HOST_PORT`, export it again or read it from `$SECRETS/install.env`, which remembers the port (the installer also prints the workbench URL).

Re-running `install` is safe: it never regenerates a password. No secret is printed.

### 2. Log in to the workbench

```sh
cat "$SECRETS/ui_password"
```

Open `http://127.0.0.1:$PORT` and log in as user `operator` with that password. From another machine, use an SSH tunnel with the same local port (the allowed origins follow `OPSPILOT_WEB_HOST_PORT`):

```sh
ssh -L "$PORT:127.0.0.1:$PORT" <user>@<control-host>
```

### 3. Configure the worker and the Observer

The worker needs the model key; the Observer needs its own read-only Prometheus account. Both files already exist as comment-only placeholders. Edit them with an editor rather than `echo`, so the key does not land in shell history:

```sh
${EDITOR:-vi} "$SECRETS/worker_env"
${EDITOR:-vi} "$SECRETS/observer_env"
```

Add these as active lines (no leading `#`); a file with only comments is refused by the next `upgrade`.

`worker_env`:

```text
DEEPSEEK_API_KEY=<your DeepSeek API key>
```

`observer_env` (username and password, or one `OPSPILOT_OBSERVER_PROMETHEUS_TOKEN=<token>` line instead):

```text
OPSPILOT_OBSERVER_PROMETHEUS_USERNAME=<read-only user>
OPSPILOT_OBSERVER_PROMETHEUS_PASSWORD=<password>
```

### 4. Start the worker and the Observer

```sh
OPSPILOT_START_AGENT=1 \
OPSPILOT_OBSERVER_PROMETHEUS_URL=http://<prometheus-host>:<port> \
  deploy/compose/install.sh upgrade
```

Before any Docker call the installer checks that `worker_env` has a non-empty `DEEPSEEK_API_KEY` and that the URL is http(s); otherwise it refuses and changes nothing. The upgrade stops all runtimes, migrates, checks the database logins and starts web, worker and Observer. Both settings are remembered in `install.env`, so later runs need only `deploy/compose/install.sh upgrade`. The Prometheus URL must be reachable from the Observer container.

Check that all three runtimes are healthy (each container's healthcheck reads its own `/readyz` health listener on container port 9464):

```sh
deploy/compose/install.sh compose ps
deploy/compose/install.sh compose logs worker
```

### 5. Route alerts to a target and send them

`$SECRETS/target_identities.json` starts as `{}`: every alert is then handed to a human without a Run. To let matching alerts open a Run, add an entry keyed by the target id. With the default tool profile `fixture`, the worker knows only the target `checkout-prod` and investigates a fixed sample data set, not your live system; any other key makes the Run's tool calls fail with `TARGET_NOT_REGISTERED`.

```json
{
  "checkout-prod": {
    "integration_id": "fixture",
    "cluster_uid": "fixture-cluster",
    "namespace": "otel-demo",
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
    "match": {"version": 1, "labels": {"alertname": "<your alert name>"}}
  }
}
```

After editing, recreate the web container (a plain `restart` keeps the old file):

```sh
deploy/compose/install.sh compose up -d --force-recreate web
```

Alerts arrive at `POST http://127.0.0.1:$PORT/intake/alertmanager` with the bearer token in `$SECRETS/alertmanager_token`. An Alertmanager on the same host can use a receiver like this:

```yaml
receivers:
  - name: opspilot
    webhook_configs:
      # replace 18080 if you changed OPSPILOT_WEB_HOST_PORT
      - url: http://127.0.0.1:18080/intake/alertmanager
        http_config:
          authorization:
            type: Bearer
            credentials_file: /path/to/alertmanager_token
```

The incident, the Run and its report then appear in the workbench. In the recorded run a synthetic Alertmanager-format alert was posted to this endpoint; the second Run completed and its report was shown in the workbench. That run differed from the steps above only in port 18180, a non-default secrets directory and lab wiring: the Observer container was attached to the kind network to reach the lab's read-only Prometheus proxy.

The `otel-demo` tool profile (`OPSPILOT_TOOL_PROFILE=otel-demo`, plus the `OPSPILOT_OTEL_*` endpoints and credentials in `worker_env`) reads a pinned OpenTelemetry Demo lab instead of the fixture.

### The OpenTelemetry Demo lab on the same host

To try OpsPilot against the pinned lab (a kind cluster running the OpenTelemetry Demo, its Prometheus, Jaeger and an Alertmanager) on one clean Ubuntu 24.04 x86_64 host, run this instead of steps 1-5. As root:

```sh
adduser --disabled-password --gecos "" opspilot
su - opspilot -c 'git clone https://github.com/kevinWangSheng/production-ops-agent.git'
/home/opspilot/production-ops-agent/scripts/kind_lab/install_linux.sh --user opspilot
su - opspilot
```

`install_linux.sh` installs pinned Docker Engine, kind, kubectl and helm, adds `opspilot` to the `docker` group and starts the lab as that user (about 5 minutes; it ends with a health report). Then, as `opspilot`:

```sh
cd production-ops-agent
python3 scripts/kind_lab.py control-secrets
${EDITOR:-vi} ~/.config/opspilot/worker_env      # add DEEPSEEK_API_KEY=<your key>
OPSPILOT_START_AGENT=1 deploy/compose/install.sh install
python3 scripts/kind_lab.py connect-control --project opspilot
```

`control-secrets` fills the secrets directory before the first install: the lab's Alertmanager token as `alertmanager_token` (so the workbench accepts the lab's alerts), the lab's Prometheus and Jaeger addresses and read-only accounts in `worker_env` and `observer_env`, the lab target `m0-otel-20260909` in `target_identities.json`, and `install.env` with the `otel-demo` profile, the Observer's Prometheus URL and workbench port `18090` (the lab frontend holds 18080). It never overwrites a value already there and prints no secret; if `install.sh install` already generated its own token, it stops and says how to give the lab that token instead. `connect-control` joins the three runtime containers to the lab's `kind` network; run it again after every `install.sh upgrade`. The workbench is then at `http://127.0.0.1:18090`, and `python3 scripts/kind_lab.py fault inject --experiment-id <id>` makes the lab's checkout alert fire after about 5-7 minutes (`fault restore --experiment-id <id>` undoes it).

### Installer settings

Set these as environment variables on `install`, `upgrade`, `uninstall` or `compose`. The values used are saved in `<secrets dir>/install.env` and reused by later runs unless you set them again (`OPSPILOT_SECRETS_DIR` itself is recorded under `${XDG_STATE_HOME:-~/.local/state}/opspilot/installs/<project>`).

| Name | Default | What it does |
|---|---|---|
| `OPSPILOT_SECRETS_DIR` | the directory this project was installed with, else `${XDG_CONFIG_HOME:-$HOME/.config}/opspilot` | Secrets and settings, outside the repository. A different directory for a project whose database volume exists is refused. |
| `OPSPILOT_PROJECT` | `$COMPOSE_PROJECT_NAME`, else `opspilot` | Compose project name (lowercase letters, digits, `-`, `_`); one install per name. |
| `OPSPILOT_WEB_HOST_PORT` | `18080` | Workbench port on `127.0.0.1`; the allowed origins follow it. |
| `OPSPILOT_IMAGE` | `opspilot:local` | Product image. The default is built from the checkout; anything else is pulled and must be pinned by digest (`<name>@sha256:<64 hex>`). |
| `OPSPILOT_TOOL_PROFILE` | `fixture` | Worker tools: `fixture` (fixed sample data, target `checkout-prod`) or `otel-demo` (the pinned OpenTelemetry Demo lab). |
| `OPSPILOT_START_AGENT` | `0` | `1` also starts the worker and the Observer; refused before any Docker call unless `worker_env` has `DEEPSEEK_API_KEY` and the Prometheus URL is http(s). |
| `OPSPILOT_OBSERVER_PROMETHEUS_URL` | empty | The Observer's read-only Prometheus base URL; required when `OPSPILOT_START_AGENT=1`. |
| `OPSPILOT_TRACE` | `off` | Worker trace export: `off`, `lab` (synthetic lab only), or `outbox` (sends ids, versions, token counts and fixed codes from the trace outbox to LangSmith; only when `worker_env` has `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT=opspilot-lab-*`, optional SaaS `LANGSMITH_ENDPOINT` and `LANGSMITH_WORKSPACE_ID`; otherwise nothing is sent and the worker's `/metrics` says why). |

### Upgrade, uninstall

```sh
git pull && deploy/compose/install.sh upgrade        # rebuild from the checkout; same secrets and data
deploy/compose/install.sh uninstall                  # remove containers; keep the database volume and secrets
deploy/compose/install.sh uninstall --purge-data     # also delete the database; asks you to type the project name
```

Upgrade order, image pinning (`OPSPILOT_IMAGE=<name>@sha256:<digest>`) and target identities: [deploy/compose/UPGRADE.md](deploy/compose/UPGRADE.md). All settings are listed at the top of [deploy/compose/install.sh](deploy/compose/install.sh).

## Further reading

1. [Portfolio walkthrough](docs/portfolio/README.md) — architecture, threat model, operations, upstream attribution, evaluation results and limitations.
2. [PRD.md](PRD.md) and [feature_list.json](feature_list.json) — user capabilities and acceptance steps.
3. [SPEC.md](SPEC.md) — scope, technology choices, implementation gates (Chinese).
4. [PRODUCT-CONSTRAINTS.md](PRODUCT-CONSTRAINTS.md) — runtime constraints on product code.
5. [Scope ADR](docs/adr/0001-readonly-investigation-boundary.md) — why production writes and release gates are excluded.
6. [ROADMAP.md](ROADMAP.md) — status table and work order (Chinese).
7. [Documentation guide](docs/README.md) and [development guide](docs/development.md) — sources of truth, running from source, tests (Chinese).
8. [M1 end-to-end demo](docs/demo/m1-end-to-end.md) — alert to investigation, recovery and postmortem on the kind lab.

## License

OpsPilot is released under the [MIT License](LICENSE). Third-party material in the repository keeps its own license: the vendored DeepSeek tokenizer (MIT, `opspilot/tools/tokenizer/LICENSE`) and the HolmesGPT text excerpts (Apache-2.0, `third_party/holmesgpt/LICENSE`); see [upstream attribution](docs/portfolio/upstream-attribution.md) for what is copied and where.
