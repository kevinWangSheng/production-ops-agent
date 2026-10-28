"""``python -m opspilot.deploy.credentials {ui-user|event-token} <name>``.

Reads one secret line from stdin and prints the web's configuration entry
for it -- ``<name>=<pbkdf2 hash>`` for ``OPSPILOT_UI_USERS`` or
``<sha256>=<actor id>`` for ``OPSPILOT_EVENT_TOKENS`` -- so the installer
writes only hashes into the web's secret files. The secret is never printed.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from opspilot.web.auth import hash_password, token_digest

__all__ = ["main"]

_USAGE = "usage: python -m opspilot.deploy.credentials {ui-user|event-token} <name>"


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
