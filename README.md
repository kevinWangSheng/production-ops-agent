# OpsPilot — Read-only Operations Investigator

English | [简体中文](README.zh-CN.md)

OpsPilot is a continuously running agent for incident and post-release investigation. It reads telemetry, never changes production, and leaves every decision to a human.

Internal process documents are written in Chinese. Where this README and the Chinese contract documents ([SPEC.md](SPEC.md), [PRODUCT-CONSTRAINTS.md](PRODUCT-CONSTRAINTS.md), the [C3 technical design](docs/design/technical-proposal-2026-09-07.md), task records) disagree, the Chinese contract documents are authoritative.

## What it does

- **Investigates.** An Alertmanager alert or a release event opens an incident and an investigation Run. The model gathers evidence through read-only, target-scoped tools, keeps competing hypotheses, and publishes a report that separates facts, hypotheses and gaps. When evidence is missing it says so, or hands the incident to a human, instead of guessing.
- **Works with a human.** In the workbench an operator reads the report, asks follow-up questions (each opens a new Run), pauses investigation globally or per target, and closes or reopens incidents. Human control always takes precedence over the agent.
- **Observes recovery independently.** After a human has handled an incident, a separate Observer process with its own read-only Prometheus account samples service health and records whether the service recovered. It never performs a recovery action.
- **Drafts reviewed postmortems.** The worker drafts a postmortem from the incident record; it becomes knowledge for later investigations only after a human approves it in the workbench.
- **Watches releases.** A version-change event starts an observation window; a delayed regression becomes an incident. OpsPilot never approves, blocks or executes a release.

What it will not do is fixed in [PRODUCT-CONSTRAINTS.md](PRODUCT-CONSTRAINTS.md): no production writes, no release gates, no execution of recovery steps.

## Status

Milestone M3 (effect and delivery acceptance) is in progress. This is a portfolio project built to production engineering standards; it is **not production-proven**.

- 7 of 11 features pass their acceptance steps in [feature_list.json](feature_list.json) (F2, F3, F6, F7, F11, F12, F13). F1, F8, F9 and F14 are open.
- Acceptance runs used a local kind cluster running the OpenTelemetry Demo with injected faults, and a real model (DeepSeek). These are integration runs, not production observation.
- The Compose install below has been run on a local Linux VM, including one real investigation through the installed stack ([evidence](docs/evidence/f9-393-installed-investigation/README.md)). A clean-host install on a fresh Linux server, the published image and the `v0.1.0` tag, a 72-hour soak and a same-conditions comparison with HolmesGPT are still open.

Current work order and evidence: [ROADMAP.md](ROADMAP.md).

## Install with Docker Compose

The install runs on the control side: one Linux host running PostgreSQL, the web workbench, the worker (investigator) and the Observer, each as its own container from one product image. The workbench is published on `127.0.0.1` only; PostgreSQL publishes no port.

You need a Linux host with Docker Engine and the Compose v2 plugin, `bash`, `python3` (standard library only, used to hash the generated credentials) and this repository checked out. Run everything as a dedicated **non-root** user that can use Docker; `install.sh` refuses to run as root. macOS with colima works for development only.

### 1. Install

```sh
git clone https://github.com/kevinWangSheng/production-ops-agent.git
cd production-ops-agent
deploy/compose/install.sh install
```

This builds the image `opspilot:local` from the checkout, generates every password and token once, migrates the database, creates the four runtime database logins and starts the web workbench. Secrets go to `~/.config/opspilot` (outside the repository, mode 0600; set `OPSPILOT_SECRETS_DIR` to choose another directory). Re-running `install` is safe: it never regenerates a password. No secret is printed.

### 2. Log in to the workbench

```sh
cat ~/.config/opspilot/ui_password
```

Open `http://127.0.0.1:18080` and log in as user `operator` with that password. From another machine, use an SSH tunnel with the same local port (the allowed origins follow `OPSPILOT_WEB_HOST_PORT`):

```sh
ssh -L 18080:127.0.0.1:18080 <user>@<control-host>
```

### 3. Configure the worker and the Observer

The worker needs the model key; the Observer needs its own read-only Prometheus account. Both files already exist as comment-only placeholders. Edit them with an editor rather than `echo`, so the key does not land in shell history:

```sh
${EDITOR:-vi} ~/.config/opspilot/worker_env
# DEEPSEEK_API_KEY=<your DeepSeek API key>

${EDITOR:-vi} ~/.config/opspilot/observer_env
# OPSPILOT_OBSERVER_PROMETHEUS_USERNAME=<read-only user>
# OPSPILOT_OBSERVER_PROMETHEUS_PASSWORD=<password>
# (or OPSPILOT_OBSERVER_PROMETHEUS_TOKEN=<token>)
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

`~/.config/opspilot/target_identities.json` starts as `{}`: every alert is then handed to a human without a Run. To let matching alerts open a Run, add an entry keyed by the target id. With the default tool profile `fixture`, the worker knows only the target `checkout-prod` and investigates a fixed sample data set, not your live system; any other key makes the Run's tool calls fail with `TARGET_NOT_REGISTERED`.

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

Alerts arrive at `POST http://127.0.0.1:18080/intake/alertmanager` with the bearer token in `~/.config/opspilot/alertmanager_token`. An Alertmanager on the same host can use a receiver like this:

```yaml
receivers:
  - name: opspilot
    webhook_configs:
      - url: http://127.0.0.1:18080/intake/alertmanager
        http_config:
          authorization:
            type: Bearer
            credentials_file: /path/to/alertmanager_token
```

The incident, the Run and its report then appear in the workbench. In the recorded run a synthetic Alertmanager-format alert was posted to this endpoint; the second Run completed and its report was shown in the workbench. That run differed from the steps above only in port 18180, a non-default secrets directory and lab wiring: the Observer container was attached to the kind network to reach the lab's read-only Prometheus proxy.

The `otel-demo` tool profile (`OPSPILOT_TOOL_PROFILE=otel-demo`, plus the `OPSPILOT_OTEL_*` endpoints and credentials in `worker_env`) reads a pinned OpenTelemetry Demo lab instead of the fixture.

### Installer settings

Set these as environment variables on `install`, `upgrade`, `uninstall` or `compose`. The values used are saved in `<secrets dir>/install.env` and reused by later runs unless you set them again (`OPSPILOT_SECRETS_DIR` itself is recorded under `${XDG_STATE_HOME:-~/.local/state}/opspilot/installs/<project>`).

| Name | Default | What it does |
|---|---|---|
| `OPSPILOT_SECRETS_DIR` | the directory this project was installed with, else `${XDG_CONFIG_HOME:-~/.config}/opspilot` | Secrets and settings, outside the repository. A different directory for a project whose database volume exists is refused. |
| `OPSPILOT_PROJECT` | `$COMPOSE_PROJECT_NAME`, else `opspilot` | Compose project name (lowercase letters, digits, `-`, `_`); one install per name. |
| `OPSPILOT_WEB_HOST_PORT` | `18080` | Workbench port on `127.0.0.1`; the allowed origins follow it. |
| `OPSPILOT_IMAGE` | `opspilot:local` | Product image. The default is built from the checkout; anything else is pulled and must be pinned by digest (`<name>@sha256:<64 hex>`). |
| `OPSPILOT_TOOL_PROFILE` | `fixture` | Worker tools: `fixture` (fixed sample data, target `checkout-prod`) or `otel-demo` (the pinned OpenTelemetry Demo lab). |
| `OPSPILOT_START_AGENT` | `0` | `1` also starts the worker and the Observer; refused before any Docker call unless `worker_env` has `DEEPSEEK_API_KEY` and the Prometheus URL is http(s). |
| `OPSPILOT_OBSERVER_PROMETHEUS_URL` | empty | The Observer's read-only Prometheus base URL; required when `OPSPILOT_START_AGENT=1`. |

### Upgrade, uninstall

```sh
git pull && deploy/compose/install.sh upgrade        # rebuild from the checkout; same secrets and data
deploy/compose/install.sh uninstall                  # remove containers; keep the database volume and secrets
deploy/compose/install.sh uninstall --purge-data     # also delete the database; asks you to type the project name
```

Upgrade order, image pinning (`OPSPILOT_IMAGE=<name>@sha256:<digest>`) and target identities: [deploy/compose/UPGRADE.md](deploy/compose/UPGRADE.md). All settings are listed at the top of [deploy/compose/install.sh](deploy/compose/install.sh).

## Further reading

1. [PRD.md](PRD.md) and [feature_list.json](feature_list.json) — user capabilities and acceptance steps.
2. [SPEC.md](SPEC.md) — scope, technology choices, implementation gates (Chinese).
3. [PRODUCT-CONSTRAINTS.md](PRODUCT-CONSTRAINTS.md) — runtime constraints on product code.
4. [Scope ADR](docs/adr/0001-readonly-investigation-boundary.md) — why production writes and release gates are excluded.
5. [ROADMAP.md](ROADMAP.md) — status table and work order (Chinese).
6. [Documentation guide](docs/README.md) and [development guide](docs/development.md) — sources of truth, running from source, tests (Chinese).
7. [M1 end-to-end demo](docs/demo/m1-end-to-end.md) — alert to investigation, recovery and postmortem on the kind lab.
