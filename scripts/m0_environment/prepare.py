"""Prepare the bounded M0 lab from a pinned, publicly downloaded Compose config."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "tmp/m0-environment"
UPSTREAM = LAB / "opentelemetry-demo-63649d6d6a59de88fb421b88c3c3a6185b6d21ad"
config = json.loads((LAB / "resolved-upstream.json").read_text())
config["name"] = "opspilot-m0"
config["networks"]["default"]["name"] = "opspilot-m0-telemetry"
config["services"]["load-generator"]["environment"][
    "LOCUST_BROWSER_TRAFFIC_ENABLED"
] = "false"
config["services"]["load-generator"]["environment"]["LOCUST_USERS"] = "2"
config["services"]["prometheus"]["command"].append("--query.lookback-delta=5m")
config["services"]["grafana"]["environment"].pop("GF_INSTALL_PLUGINS", None)
ports = {
    "frontend-proxy": (18080, 8080),
    "prometheus": (19090, 9090),
    "jaeger": (16686, 16686),
    "opensearch": (19200, 9200),
}
for name, service in config["services"].items():
    service.pop("build", None)
    service["container_name"] = "opspilot-m0-" + name
    service["restart"] = "no"
    service.pop("ports", None)
    if name in ports:
        published, target = ports[name]
        service["ports"] = [
            {
                "target": target,
                "published": str(published),
                "host_ip": "127.0.0.1",
                "protocol": "tcp",
            }
        ]
    for volume in service.get("volumes", []):
        if name != "flagd-ui":
            volume["read_only"] = True
    env = service.get("environment", {})
    if "OTEL_RESOURCE_ATTRIBUTES" in env:
        env["OTEL_RESOURCE_ATTRIBUTES"] += ",opspilot.integration.id=m0-otel-20260909"
collector = config["services"]["otel-collector"]
collector["volumes"] = [
    v
    for v in collector["volumes"]
    if v["target"] not in ("/hostfs", "/var/run/docker.sock")
]
source = (UPSTREAM / "src/otel-collector/otelcol-config.yml").read_text()
start = source.index("  docker_stats:")
end = source.index("  redis:", start)
source = source[:start] + source[end:]
start = source.index("  # Host metrics")
end = source.index("\nexporters:", start)
source = source[:start] + source[end:]
source = source.replace("[hostmetrics, docker_stats, httpcheck/", "[httpcheck/")
(LAB / "collector.yml").write_text(source)
for volume in collector["volumes"]:
    if volume["target"] == "/etc/otelcol-config.yml":
        volume["source"] = str(LAB / "collector.yml")
# Preserve backend data across container replacement; no deletion during this task.
config["volumes"] = {"prometheus-data": {}, "opensearch-data": {}}
for service, target in [
    ("prometheus", "/prometheus"),
    ("opensearch", "/usr/share/opensearch/data"),
]:
    config["services"][service].setdefault("volumes", []).append(
        {"type": "volume", "source": service + "-data", "target": target}
    )
prom_source = (UPSTREAM / "src/prometheus/prometheus-config.yaml").read_text()
prom_source = prom_source.replace(
    "    - service.name\n",
    "    - opspilot.integration.id\n    - service.version\n    - service.name\n",
)
(LAB / "prometheus.yaml").write_text(prom_source)
for volume in config["services"]["prometheus"]["volumes"]:
    if volume["target"] == "/etc/prometheus/prometheus-config.yaml":
        volume["source"] = str(LAB / "prometheus.yaml")
(LAB / "compose.json").write_text(json.dumps(config, indent=2) + "\n")
print(
    "Prepared pinned-version lab configuration; image digests still require pull verification."
)
