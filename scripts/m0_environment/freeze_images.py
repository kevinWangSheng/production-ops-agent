"""Verify the archived lab inputs before materializing its runtime files.

Committed image/configuration manifests are inputs, never output destinations.
A changed experiment needs its own reviewed record instead of rewriting this one.
"""

import argparse
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path


def reject_symlinks(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise SystemExit(
            f"Unexpected symlink in input/output path: {path}; no files written."
        )


def validate_bind_inputs(config, generated):
    source_name = "opentelemetry-demo-63649d6d6a59de88fb421b88c3c3a6185b6d21ad"
    source_root = LAB / source_name
    bundle_path = LAB / "otel.tar.gz"
    manifest_path = evidence / "source-downloads.json"
    for path in (bundle_path, manifest_path):
        reject_symlinks(path)
    sources = [
        entry
        for entry in json.loads(manifest_path.read_text())
        if entry.get("name") == "otel"
    ]
    expected_url = "https://codeload.github.com/open-telemetry/opentelemetry-demo/tar.gz/63649d6d6a59de88fb421b88c3c3a6185b6d21ad"
    if len(sources) != 1 or sources[0].get("url") != expected_url:
        raise SystemExit(
            "Fixed OTel source manifest missing or incompatible; no files written."
        )
    if hashlib.sha256(bundle_path.read_bytes()).hexdigest() != sources[0]["sha256"]:
        raise SystemExit(
            "Source archive differs from its recorded SHA256; no files written."
        )
    bind_count = 0
    with tarfile.open(bundle_path, "r:gz") as bundle:
        members = {member.name.rstrip("/"): member for member in bundle.getmembers()}
        for service in config["services"].values():
            for volume in service.get("volumes", []):
                if volume.get("type") != "bind":
                    continue
                path = Path(volume["source"])
                # Classify lexically before stat/open: unknown sources are not read.
                if not path.is_absolute() or ".." in path.parts:
                    raise SystemExit("Unapproved bind source; no files written.")
                if path not in generated and not path.is_relative_to(source_root):
                    raise SystemExit(
                        f"Unapproved bind source: {path}; no files written."
                    )
                reject_symlinks(path)
                bind_count += 1
                if path in generated:
                    if path.exists() and not path.is_file():
                        raise SystemExit(
                            "Generated bind source is not a file; no files written."
                        )
                    continue  # Candidate bytes are checked by the archived four-file hashes.
                prefix = path.relative_to(LAB).as_posix()
                expected = {}
                for name, member in members.items():
                    if name != prefix and not name.startswith(prefix + "/"):
                        continue
                    if member.isdir():
                        expected[name] = ("directory", None)
                    elif member.isfile():
                        expected[name] = (
                            "file",
                            hashlib.sha256(
                                bundle.extractfile(member).read()
                            ).hexdigest(),
                        )
                    else:
                        raise SystemExit(
                            "Unsupported source archive entry in bind; no files written."
                        )
                if not expected:
                    raise SystemExit(
                        "Bind is absent from fixed source archive; no files written."
                    )
                actual = {}
                for entry in (path, *path.rglob("*")):
                    reject_symlinks(entry)
                    name = entry.relative_to(LAB).as_posix()
                    if name not in expected:
                        raise SystemExit(
                            "Unexpected bind directory member; no files written."
                        )
                    if entry.is_dir():
                        actual[name] = ("directory", None)
                    elif entry.is_file():
                        actual[name] = (
                            "file",
                            hashlib.sha256(entry.read_bytes()).hexdigest(),
                        )
                    else:
                        raise SystemExit(
                            "Unsupported or missing bind entry; no files written."
                        )
                if actual != expected:
                    raise SystemExit(
                        f"Bind source differs from fixed archive: {path}; no files written."
                    )
    return bind_count


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--check-only",
    action="store_true",
    help="Validate all inputs without writing runtime files",
)
args = parser.parse_args()

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "tmp/m0-environment"
evidence = ROOT / "docs/evidence/m0-real-environment"
archive_paths = [
    evidence / name for name in ("image-lock.json", "configuration-hashes.json")
]
for path in archive_paths:
    reject_symlinks(path)
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
for path in (
    LAB / "collector.yml",
    LAB / "prometheus.yaml",
    LAB / "read_proxy.py",
    LAB / "compose-pinned.json",
    ROOT / "scripts/m0_environment/read_proxy.py",
):
    reject_symlinks(path)
bind_count = validate_bind_inputs(
    config,
    {LAB / name for name in ("collector.yml", "prometheus.yaml", "read_proxy.py")},
)
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

if not args.check_only:
    for path, content in outputs.items():
        path.write_bytes(content)
print(
    f"Verified candidate: {len(records)} archived images, {bind_count} bind inputs and configuration; "
    f"runtime {'unchanged (check-only)' if args.check_only else 'materialized'}, evidence unchanged."
)
