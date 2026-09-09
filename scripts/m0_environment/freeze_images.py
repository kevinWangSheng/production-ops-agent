"""Freeze all pulled lab images before the first workload startup."""

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "tmp/m0-environment"
config = json.loads((LAB / "compose.json").read_text())
config["networks"]["investigation"] = {
    "name": "opspilot-m0-investigation",
    "internal": True,
}
# Only the proxy joins both the workload network and the investigation network.
# The investigator must join only the latter and have no Docker/socket mount.
config["services"]["read-proxy"] = {
    "image": "python:3.12.12-slim",
    "container_name": "opspilot-m0-read-proxy",
    "command": ["python", "/app/read_proxy.py"],
    "user": "65534:65534",
    "read_only": True,
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "networks": {"default": {}, "investigation": {}},
    "environment": {
        "BIND_ADDRESS": "0.0.0.0",
        "PROMETHEUS_URL": "http://prometheus:9090",
        "JAEGER_URL": "http://jaeger:16686",
        "OPENSEARCH_URL": "http://opensearch:9200",
    },
    "volumes": [
        {
            "type": "bind",
            "source": str(LAB / "read_proxy.py"),
            "target": "/app/read_proxy.py",
            "read_only": True,
        }
    ],
    "ports": [{"target": 18081, "published": "18081", "host_ip": "127.0.0.1"}],
    "deploy": {"resources": {"limits": {"memory": "64M", "cpus": "0.5"}}},
}
(LAB / "read_proxy.py").write_bytes(
    (ROOT / "scripts/m0_environment/read_proxy.py").read_bytes()
)
records = []
for name, service in config["services"].items():
    tag = service["image"]
    raw = subprocess.check_output(
        ["docker", "--context", "colima-m0-otel", "image", "inspect", tag], text=True
    )
    image = json.loads(raw)[0]
    digest = image["RepoDigests"][0]
    service["image"] = digest
    records.append(
        {
            "service": name,
            "tag": tag,
            "repo_digest": digest,
            "image_id": image["Id"],
            "architecture": image["Architecture"],
            "size": image["Size"],
        }
    )
(LAB / "compose-pinned.json").write_text(json.dumps(config, indent=2) + "\n")
evidence = ROOT / "docs/evidence/m0-real-environment"
(evidence / "image-lock.json").write_text(json.dumps(records, indent=2) + "\n")
files = [
    LAB / name
    for name in (
        "compose-pinned.json",
        "collector.yml",
        "prometheus.yaml",
        "read_proxy.py",
    )
]
(evidence / "configuration-hashes.json").write_text(
    json.dumps(
        {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files
        },
        indent=2,
    )
    + "\n"
)
print(f"Frozen {len(records)} images by digest before startup.")
