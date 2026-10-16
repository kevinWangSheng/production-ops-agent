"""Record lab readonly authorization results; never repairs the lab."""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.kind_lab import LAB_DIR, OPENER, READ_PATHS, ROLE_ENV_VARS

BASE_URL = "http://127.0.0.1:19090"
READ_REQUESTS = tuple(("GET", p) for p in READ_PATHS) + (
    ("GET", "/api/v1/label/__name__/values"),
)
DENIED_REQUESTS = (
    ("POST", "/api/v1/otlp/v1/metrics"),
    ("POST", "/api/v1/write"),
    ("POST", "/api/v1/admin/tsdb/snapshot"),
    ("POST", "/-/reload"),
    ("GET", "/federate"),
    ("POST", "/api/v1/query"),
)
MAX_BYTES = 2 * 1024 * 1024


def authorization(role: str, directory: Path) -> str:
    # Parse assignments, never execute/source shell code.
    values = {}
    for line in (directory / f"prometheus-{role}.env").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        parsed = shlex.split(value)
        values[key] = parsed[0] if len(parsed) == 1 else ""
    user_key, password_key = ROLE_ENV_VARS[role]
    if values.get(user_key) != role or not values.get(password_key):
        raise ValueError("invalid role env file")
    return (
        "Basic " + base64.b64encode(f"{role}:{values[password_key]}".encode()).decode()
    )


def request(
    base_url: str, method: str, path: str, auth: str | None
) -> tuple[int, bytes]:
    headers = {} if auth is None else {"Authorization": auth}
    if method == "POST":
        headers["Content-Type"] = "application/x-protobuf"
    req = urllib.request.Request(
        base_url + path,
        data=b"" if method == "POST" else None,
        headers=headers,
        method=method,
    )
    try:
        with OPENER.open(req, timeout=15) as response:
            return int(response.status), response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


def latest_samples(base_url: str, auth: str) -> tuple[int, dict[str, float] | None]:
    # A raw range vector retains SAMPLE timestamps. Instant-vector timestamps
    # are evaluation times and cannot detect an unauthorized write.
    path = "/api/v1/query?" + urllib.parse.urlencode(
        {"query": "opspilot_f7_probe[30d]"}
    )
    status, body = request(base_url, "GET", path, auth)
    if status != 200 or len(body) > MAX_BYTES:
        return status, None
    try:
        payload = json.loads(body)
        if payload["status"] != "success" or payload["data"]["resultType"] != "matrix":
            return status, None
        latest = {}
        for series in payload["data"]["result"]:
            key = json.dumps(series["metric"], sort_keys=True)
            latest[key] = max(float(value[0]) for value in series["values"])
        return status, latest
    except (KeyError, TypeError, ValueError):
        return status, None


def probe(base_url: str = BASE_URL, directory: Path = LAB_DIR) -> dict[str, Any]:
    auths = {role: authorization(role, directory) for role in ROLE_ENV_VARS}
    before_status, before = latest_samples(base_url, auths["lab"])
    results = []
    for role in ("investigator", "observer", "anonymous"):
        for method, path in READ_REQUESTS + DENIED_REQUESTS:
            query = (
                "?query=up" if path in ("/api/v1/query", "/api/v1/query_range") else ""
            )
            if path == "/api/v1/query_range":
                query += "&start=0&end=1&step=1"
            elif path == "/api/v1/series":
                query = "?match%5B%5D=up"
            expected = (
                401
                if role == "anonymous"
                else (200 if (method, path) in READ_REQUESTS else 403)
            )
            status, _ = request(base_url, method, path + query, auths.get(role))
            results.append(
                {
                    "method": method,
                    "path": path,
                    "account": role,
                    "status": status,
                    "expected": expected,
                    "matches": status == expected,
                }
            )
    after_status, after = latest_samples(base_url, auths["lab"])
    unchanged = before is not None and after is not None and before == after
    sample_check = {
        "account": "lab",
        "method": "GET",
        "path": "/api/v1/query",
        "status_before": before_status,
        "status_after": after_status,
        "expected": "no new sample timestamps",
        "matches": unchanged,
        "series_before": None if before is None else len(before),
        "series_after": None if after is None else len(after),
    }
    return {
        "requests": results,
        "sample_check": sample_check,
        "ok": all(r["matches"] for r in results) and unchanged,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-dir", type=Path, default=LAB_DIR)
    args = parser.parse_args(argv)
    try:
        report = probe(directory=args.env_dir)
    except (OSError, ValueError):
        print(json.dumps({"ok": False, "error": "cannot read role credentials"}))
        return 1
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
