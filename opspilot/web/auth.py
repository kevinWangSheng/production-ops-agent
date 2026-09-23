"""Trusted authentication adapter for the two intake channels (C3 section 9).

``ui_basic`` is named Basic Auth with hashed passwords behind the TLS proxy;
``event_token`` is the separate bearer token for event delivery. Each verifier
turns a successful check into an ``opspilot.intake.Principal`` and nothing
else: the presented credential is never stored on the principal, never
rendered into an error, and never compared with ``==``.

Forged identity headers (``X-Forwarded-User`` and friends) are simply never
read here. Stripping them is the proxy's job; ignoring them is this module's.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from opspilot.intake import AuthChannel, Principal

_PBKDF2_ALGORITHM = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 200_000
#: A verifier that always fails but costs one real hash, so an unknown user
#: name is not distinguishable from a wrong password by timing.
_DECOY_HASH = hashlib.pbkdf2_hmac(
    "sha256", b"decoy", b"decoy-salt", _PBKDF2_ITERATIONS
).hex()
_MAX_HEADER_BYTES = 4096


class AuthError(Exception):
    """Fixed-code refusal. ``code`` is safe to render; nothing else is kept."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Encode a password for ``AuthConfig.ui_users``; never store plaintext."""
    if not isinstance(password, str) or not password:
        raise ValueError("INVALID_INPUT")
    salt = secrets.token_bytes(16) if salt is None else salt
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"{_PBKDF2_ALGORITHM}${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        salt = bytes.fromhex(salt_hex)
        rounds = int(iterations)
    except (ValueError, AttributeError):
        return False
    if algorithm != _PBKDF2_ALGORITHM or rounds < 1:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(candidate.hex(), digest_hex)


def token_digest(token: str) -> str:
    """Digest under which an event token is configured; the token itself is not."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthConfig:
    """Hashed credentials only. ``event_tokens`` maps sha256(token) -> actor id."""

    ui_users: Mapping[str, str]
    event_tokens: Mapping[str, str]
    auth_revision: str
    allowed_origins: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.auth_revision, str) or not self.auth_revision:
            raise ValueError("INVALID_INPUT")
        for encoded in self.ui_users.values():
            if not encoded.startswith(_PBKDF2_ALGORITHM + "$"):
                raise ValueError("UI_PASSWORD_MUST_BE_HASHED")
        for digest in self.event_tokens:
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("EVENT_TOKEN_MUST_BE_SHA256_HEX")


class Authenticator:
    """Verify one request's headers for one channel."""

    def __init__(self, config: AuthConfig) -> None:
        self._config = config

    def ui_principal(self, headers: Mapping[str, str]) -> Principal:
        scheme, credential = _authorization(headers)
        if scheme != "basic":
            raise AuthError("MISSING_CREDENTIALS")
        try:
            decoded = base64.b64decode(credential, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise AuthError("INVALID_CREDENTIALS") from None
        user, separator, password = decoded.partition(":")
        if not separator or not user:
            raise AuthError("INVALID_CREDENTIALS")
        encoded = self._config.ui_users.get(user)
        if encoded is None:
            # Burn one real verification so a missing user costs the same.
            verify_password(
                password,
                f"{_PBKDF2_ALGORITHM}${_PBKDF2_ITERATIONS}$"
                f"{b'decoy-salt'.hex()}${_DECOY_HASH}",
            )
            raise AuthError("INVALID_CREDENTIALS")
        if not verify_password(password, encoded):
            raise AuthError("INVALID_CREDENTIALS")
        return self._principal(user, "ui_basic")

    def event_principal(self, headers: Mapping[str, str]) -> Principal:
        scheme, credential = _authorization(headers)
        if scheme != "bearer":
            raise AuthError("MISSING_CREDENTIALS")
        presented = token_digest(credential)
        actor: str | None = None
        for digest, candidate in self._config.event_tokens.items():
            # Compare every entry so the match position does not leak.
            if hmac.compare_digest(presented, digest):
                actor = candidate
        if actor is None:
            raise AuthError("INVALID_CREDENTIALS")
        return self._principal(actor, "event_token")

    def check_origin(self, headers: Mapping[str, str]) -> None:
        """Refuse a state-changing browser request from a foreign origin.

        The browser sets ``Origin`` on every form POST and ``Sec-Fetch-Site``
        on every request; a cross-site page cannot forge either. Both absent
        is treated as foreign, not as trusted.
        """
        origin = headers.get("origin")
        if origin is not None:
            if origin in self._config.allowed_origins:
                return
            raise AuthError("ORIGIN_REJECTED")
        if headers.get("sec-fetch-site") == "same-origin":
            return
        raise AuthError("ORIGIN_REJECTED")

    def _principal(self, actor: str, channel: AuthChannel) -> Principal:
        try:
            return Principal(
                actor_id=actor,
                channel=channel,
                auth_revision=self._config.auth_revision,
            )
        except ValueError:
            # A configured actor id that the contract refuses is a deployment
            # error; it must not become a half-trusted identity.
            raise AuthError("INVALID_CREDENTIALS") from None


def _authorization(headers: Mapping[str, str]) -> tuple[str, str]:
    value = headers.get("authorization")
    if value is None:
        raise AuthError("MISSING_CREDENTIALS")
    if len(value) > _MAX_HEADER_BYTES:
        raise AuthError("INVALID_CREDENTIALS")
    scheme, _, credential = value.strip().partition(" ")
    credential = credential.strip()
    if not credential:
        raise AuthError("MISSING_CREDENTIALS")
    return scheme.lower(), credential
