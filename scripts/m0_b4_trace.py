"""Send/read one safe LangSmith run for an already completed B4 probe.

Credentials are read only by the trusted client process from the existing
private env file; provider responses, prompts and private fields are never
printed or persisted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx2


def safe_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def load_private_env(path: Path) -> dict[str, str]:
    """Read only simple KEY=VALUE entries; never return or print secrets."""
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


async def export(run_id: str, summary_path: Path, output_path: Path) -> dict:
    summary = json.loads(summary_path.read_text())
    env_path = Path("/Users/shenghuikevin/dev/AI/production-ops-agent/.env")
    values = load_private_env(env_path)
    endpoint = values.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    project = values.get("LANGSMITH_PROJECT")
    api_key = values.get("LANGSMITH_API_KEY")
    if (
        not project
        or not api_key
        or endpoint
        not in {
            "https://api.smith.langchain.com",
            "https://eu.api.smith.langchain.com",
        }
    ):
        raise RuntimeError("LANGSMITH_CONFIG_UNAVAILABLE")
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "id": run_id,
        "name": "m0-b4-recovery",
        "run_type": "chain",
        "project_name": project,
        "inputs": {"contract": "round-05-recovery", "run_id": run_id},
        "outputs": {
            "status": summary.get("status"),
            "evidence_preserved": summary.get("evidence_preserved"),
            "message_pairing": summary.get("message_pairing"),
            "final_contract": summary.get("final_contract"),
        },
        "start_time": now,
        "end_time": now,
        "tags": [],
        "extra": {},
    }
    headers = {"x-api-key": api_key, "content-type": "application/json"}
    workspace = values.get("LANGSMITH_WORKSPACE_ID")
    if workspace:
        headers["x-tenant-id"] = workspace
    async with httpx2.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=20
    ) as client:
        post = await client.post(endpoint + "/runs", headers=headers, json=payload)
        if post.status_code not in (200, 201, 202, 409):
            raise RuntimeError(f"TRACE_POST_HTTP_{post.status_code}")
        returned = None
        read_status = None
        for _ in range(3):
            read = await client.get(endpoint + "/runs/" + run_id, headers=headers)
            read_status = read.status_code
            if read_status == 200:
                returned = read.json()
                break
            if read_status != 404:
                raise RuntimeError(f"TRACE_READ_HTTP_{read_status}")
            await asyncio.sleep(2)
        if returned is None:
            raise RuntimeError(f"TRACE_READ_HTTP_{read_status}")
    if not isinstance(returned, dict):
        raise RuntimeError("TRACE_READ_INVALID")
    allowed = {"id", "session_id", "name", "run_type", "inputs", "outputs"}
    observed = {key: returned.get(key) for key in allowed}
    if observed["id"] != run_id or observed["inputs"] != payload["inputs"]:
        raise RuntimeError("TRACE_IDENTITY_OR_INPUT_MISMATCH")
    if observed["outputs"] != payload["outputs"]:
        raise RuntimeError("TRACE_OUTPUT_MISMATCH")
    result = {
        "status": "TRACE_VERIFIED",
        "run_id": run_id,
        "project_name_hash": safe_hash(project),
        "payload_hash": safe_hash(payload),
        "returned_fields": sorted(k for k, v in observed.items() if v is not None),
        "private_fields_persisted": False,
        "model_http": 0,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    output_path.chmod(0o600)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(export(args.run_id, args.summary, args.output))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
