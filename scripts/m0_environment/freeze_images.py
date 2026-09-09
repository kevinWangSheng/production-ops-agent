"""Verify the archived lab inputs before materializing its runtime files.

Committed image/configuration manifests are inputs, never output destinations.
A changed experiment needs its own reviewed record instead of rewriting this one.
"""

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "tmp/m0-environment"
evidence = ROOT / "docs/evidence/m0-real-environment"
archive_paths = [
    evidence / name for name in ("image-lock.json", "configuration-hashes.json")
]
if not all(path.is_file() for path in archive_paths):
    raise SystemExit(
        "Archived image lock and configuration hashes are required; no files written."
    )
expected_images = json.loads(archive_paths[0].read_text())
expected_hashes = json.loads(archive_paths[1].read_text())
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
proxy_bytes = (ROOT / "scripts/m0_environment/read_proxy.py").read_bytes()
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
if records != expected_images:
    raise SystemExit(
        "Image identity/platform differs from the archived lock; no files written."
    )

# Build and check the entire candidate before changing even the runtime proxy.
outputs = {
    LAB / "compose-pinned.json": (json.dumps(config, indent=2) + "\n").encode(),
    LAB / "read_proxy.py": proxy_bytes,
}
inputs = {
    **outputs,
    **{
        LAB / name: (LAB / name).read_bytes()
        for name in ("collector.yml", "prometheus.yaml")
    },
}
hashes = {
    str(path.relative_to(ROOT)): hashlib.sha256(content).hexdigest()
    for path, content in inputs.items()
}
if hashes != expected_hashes:
    raise SystemExit(
        "Configuration differs from the archived hashes; no files written."
    )

for path, content in outputs.items():
    path.write_bytes(content)
print(
    f"Verified {len(records)} archived images/configuration; runtime materialized, evidence unchanged."
)
