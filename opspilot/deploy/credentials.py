"""``python -m opspilot.deploy.credentials {ui-user|event-token} <name>``.

Reads one secret line from stdin and prints the web's configuration entry
for it -- ``<name>=<pbkdf2 hash>`` for ``OPSPILOT_UI_USERS`` or
``<sha256>=<actor id>`` for ``OPSPILOT_EVENT_TOKENS`` -- so the installer
writes only hashes into the web's secret files. The secret is never printed.

Standard library only, so the installer runs it with the host's ``python3``
(``python3 -I opspilot/deploy/credentials.py``) before any image exists; the
encoding is the one ``opspilot.web.auth`` verifies, which
``tests/test_392_credentials_impl.py`` checks against that module.
"""

from __future__ import annotations

import hashlib
import secrets
import sys
from collections.abc import Sequence

__all__ = ["main"]

# Same values as opspilot.web.auth (checked by the tests).
PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 200_000

_USAGE = "usage: python -m opspilot.deploy.credentials {ui-user|event-token} <name>"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in ("ui-user", "event-token"):
        print(_USAGE, file=sys.stderr)
        return 2
    kind, name = args
    # The web splits entries on "," and "=" (opspilot.web.__main__._pairs).
    if not name or any(char in name for char in ",= \t\n"):
        print(f"{kind}: name must be non-empty without , = or spaces", file=sys.stderr)
        return 2
    secret = sys.stdin.readline().rstrip("\n")
    if not secret:
        print(f"{kind}: empty secret on stdin", file=sys.stderr)
        return 1
    if kind == "ui-user":
        print(f"{name}={hash_password(secret)}")
    else:
        print(f"{token_digest(secret)}={name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
