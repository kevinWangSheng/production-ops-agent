"""Create the product's read-only Elasticsearch/OpenSearch identity (B1-5, #671).

Engineer-only setup for a lab or rental host: with the engineer's (admin)
credentials, create the read-only identity the worker's ``es-ro`` slot uses,
and write the worker settings to a private file (mode 600) under LAB_DIR.
Nothing secret is printed or put on a command line.

* Elasticsearch 8: an API key with ``cluster: [monitor]`` and, on every index
  (``*``), ``read``, ``view_index_metadata`` and ``monitor``. The file gets
  ``OPSPILOT_ES_URL`` and ``OPSPILOT_ES_API_KEY`` (the key's ``encoded``
  form).
* OpenSearch 2: an internal user with the built-in ``readall_and_monitor``
  role and a generated password (OpenSearch has no ApiKey). The file gets
  ``OPSPILOT_ES_URL``, ``OPSPILOT_ES_USERNAME`` and ``OPSPILOT_ES_PASSWORD``.
  The role mapping through ``opendistro_security_roles`` is unverified until
  the #671 smoke run (B1-19).

The engineer's credentials come only from the environment, under the names
the upstream evaluation fixtures use: ``ELASTICSEARCH_URL`` with
``ELASTICSEARCH_API_KEY``, or ``ELASTICSEARCH_USERNAME`` and
``ELASTICSEARCH_PASSWORD``. They stay separate from the product's key: the
file this script writes never contains them.

Usage::

    ELASTICSEARCH_URL=http://127.0.0.1:9200 ELASTICSEARCH_API_KEY=... \\
        .venv/bin/python scripts/es_readonly_identity.py --backend elasticsearch

The worker then reads the file's three lines as its ``OPSPILOT_ES_*``
environment (for example through ``OPSPILOT_ENV_FILE`` or ``set -a; .``).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import ssl
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
#: kind_lab's LAB_DIR (``tmp/m1-kind-lab``), not imported so the script runs
#: without the lab's tooling on a rental host.
LAB_DIR = ROOT / "tmp" / "m1-kind-lab"
DEFAULT_OUT = LAB_DIR / "es-readonly.env"
DEFAULT_NAME = "opspilot-investigator"

#: B1-5: what the product's identity may do.
ES_ROLE_DESCRIPTORS: dict[str, Any] = {
    "opspilot_readonly": {
        "cluster": ["monitor"],
        "indices": [
            {
                "names": ["*"],
                "privileges": ["read", "view_index_metadata", "monitor"],
            }
        ],
    }
}
OPENSEARCH_ROLE = "readall_and_monitor"


class SetupError(Exception):
    """A fixed message; never carries a credential or a response body."""


def _engineer_auth(env: dict[str, str]) -> str:
    key = env.get("ELASTICSEARCH_API_KEY") or ""
    user = env.get("ELASTICSEARCH_USERNAME") or ""
    password = env.get("ELASTICSEARCH_PASSWORD") or ""
    if key and (user or password):
        raise SetupError(
            "set ELASTICSEARCH_API_KEY or ELASTICSEARCH_USERNAME/PASSWORD, not both"
        )
    if key:
        return f"ApiKey {key}"
    if user and password:
        pair = f"{user}:{password}".encode()
        return "Basic " + base64.b64encode(pair).decode("ascii")
    raise SetupError(
        "engineer credentials missing: ELASTICSEARCH_API_KEY, or "
        "ELASTICSEARCH_USERNAME and ELASTICSEARCH_PASSWORD"
    )


def _opener(ca_file: str | None) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if ca_file:
        handlers.append(
            urllib.request.HTTPSHandler(
                context=ssl.create_default_context(cafile=ca_file)
            )
        )
    return urllib.request.build_opener(*handlers)


def _call(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    auth: str,
    body: Any,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with opener.open(request, timeout=30) as response:
            payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as error:
        kind = "unknown"
        try:
            detail = json.loads(error.read(8192).decode("utf-8"))
            if isinstance(detail.get("error"), dict):
                kind = str(detail["error"].get("type") or kind)
        except (ValueError, UnicodeDecodeError, AttributeError):
            pass
        raise SetupError(
            f"{method} {urllib.parse.urlsplit(url).path}: HTTP {error.code} ({kind})"
        ) from None
    except (urllib.error.URLError, OSError) as error:
        raise SetupError(
            f"{method} {urllib.parse.urlsplit(url).path}: unreachable ({type(error).__name__})"
        ) from None
    if not isinstance(payload, dict):
        raise SetupError("unexpected response shape")
    return payload


def _private_write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def create_elasticsearch_key(
    opener: urllib.request.OpenerDirector,
    engineer_url: str,
    auth: str,
    *,
    name: str,
    expiration: str,
) -> tuple[str, str]:
    """``(key id, encoded key)`` of a new B1-5 API key."""
    payload = _call(
        opener,
        "POST",
        f"{engineer_url}/_security/api_key",
        auth,
        {
            "name": name,
            "expiration": expiration,
            "role_descriptors": ES_ROLE_DESCRIPTORS,
        },
    )
    key_id = payload.get("id")
    encoded = payload.get("encoded")
    if not isinstance(encoded, str) or not encoded:
        raw = payload.get("api_key")
        if isinstance(key_id, str) and isinstance(raw, str):
            encoded = base64.b64encode(f"{key_id}:{raw}".encode()).decode("ascii")
    if not isinstance(key_id, str) or not isinstance(encoded, str) or not encoded:
        raise SetupError("the API key response has no id/encoded value")
    return key_id, encoded


def create_opensearch_user(
    opener: urllib.request.OpenerDirector,
    engineer_url: str,
    auth: str,
    *,
    name: str,
) -> str:
    """The generated password of a new ``readall_and_monitor`` user."""
    password = secrets.token_urlsafe(24) + "aA1!"
    _call(
        opener,
        "PUT",
        f"{engineer_url}/_plugins/_security/api/internalusers/{urllib.parse.quote(name)}",
        auth,
        {"password": password, "opendistro_security_roles": [OPENSEARCH_ROLE]},
    )
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--backend", choices=("elasticsearch", "opensearch"), required=True
    )
    parser.add_argument(
        "--product-url",
        help="OPSPILOT_ES_URL written to the file (default ELASTICSEARCH_URL)",
    )
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument(
        "--expiration", default="7d", help="Elasticsearch API key lifetime"
    )
    parser.add_argument("--ca-file", help="CA for an HTTPS engineer URL")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    env = dict(os.environ)
    try:
        engineer_url = (env.get("ELASTICSEARCH_URL") or "").rstrip("/")
        if not engineer_url:
            raise SetupError("ELASTICSEARCH_URL is not set")
        auth = _engineer_auth(env)
        opener = _opener(args.ca_file)
        product_url = (args.product_url or engineer_url).rstrip("/")
        if args.backend == "elasticsearch":
            key_id, encoded = create_elasticsearch_key(
                opener, engineer_url, auth, name=args.name, expiration=args.expiration
            )
            lines = [f"OPSPILOT_ES_URL={product_url}", f"OPSPILOT_ES_API_KEY={encoded}"]
            identity = (
                f"API key id {key_id} ({args.name}, expires in {args.expiration})"
            )
        else:
            password = create_opensearch_user(
                opener, engineer_url, auth, name=args.name
            )
            lines = [
                f"OPSPILOT_ES_URL={product_url}",
                f"OPSPILOT_ES_USERNAME={args.name}",
                f"OPSPILOT_ES_PASSWORD={password}",
            ]
            identity = f"internal user {args.name} with role {OPENSEARCH_ROLE}"
        if args.ca_file and product_url.startswith("https:"):
            lines.append(f"OPSPILOT_ES_CA_FILE={args.ca_file}")
        _private_write(args.out, lines)
    except SetupError as error:
        print(f"es_readonly_identity: {error}", file=sys.stderr)
        return 1
    print(
        f"es_readonly_identity: created {identity}; settings written to {args.out} (mode 600)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
