"""Pure credential redaction shared by intake, observation and tool registration."""

from __future__ import annotations

import json
import re

# The authentication-bearing subset of RESERVED_PARAMETERS. These names may
# not appear as target selector keys either: unlike "target"/"target_id",
# which are targeting words a selector could plausibly need, every name here
# denotes credential material, which belongs behind `credential_ref`.
_AUTHENTICATION_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "credential",
        "credential_ref",
        "headers",
        "password",
        "secret",
        "token",
    }
)

# A flat denylist cannot hold the invariant on its own: `X-Api-Key`,
# `access_token` and `proxy_authorization` are conventional authentication
# fields that no normalization of the names above would ever reach (bot
# review finding). Two rules cover the alias families without swallowing
# legitimate query parameters:
#
# * unambiguous credential words are matched against the name folded to
#   alphanumerics, so separators and capitalisation cannot hide them;
# * short words that are also ordinary English fragments are matched only as
#   whole tokens, split on separators and camelCase. "auth" as a token is an
#   authentication field; "auth" inside `author_filter` is not, and that
#   distinction is what a plain substring rule got wrong (caught by
#   test_ordinary_parameter_names_are_still_accepted).
#
# This is a rule about parameter *names*, not about prose: it does not
# contradict the deliberate decision (see ToolDescription) to keep
# secret-*value* detection a human-review question. A legitimate read-only
# query parameter has no business being named after a credential, and the
# refusal is a fixed-code registration error the operator sees immediately.
_AUTHENTICATION_SUBSTRINGS = (
    "accesskey",
    "accesstoken",
    "apikey",
    "authorisation",
    "authorization",
    "bearer",
    "certificate",
    "credential",
    "keypair",
    "keystore",
    "passwd",
    "password",
    "privatekey",
    "publickey",
    "secret",
    "signature",
    "sshkey",
    "token",
    "truststore",
)
_AUTHENTICATION_TOKENS = frozenset(
    {"auth", "cert", "cookie", "p12", "pem", "pfx", "sessionid", "sessionids", "sig"}
)
# ``session`` names a credential only as a name's last word or before
# ``id``/``key``/``cookie`` (#366, B1-U3 a; plurals too, so ``session_ids``
# stays hidden): ``session``, ``session_id``, ``sessionKey`` are session
# credentials; ``sessionAffinity``, ``SESSION_TIMEOUT``,
# ``session_timeout_ms`` and ``ssoSessionIdleTimeout`` are configuration
# about sessions.
_SESSION_CREDENTIAL_NEXT = frozenset({"id", "ids", "key", "keys", "cookie", "cookies"})
_WORDS = re.compile(r"[A-Za-z][a-z0-9]*|[0-9]+")


def _authentication_name(name: str) -> bool:
    folded = "".join(char for char in name.lower() if char.isalnum())
    if any(stem in folded for stem in _AUTHENTICATION_SUBSTRINGS):
        return True
    # Bare ``key`` is deliberately absent from both collections: `label_key`,
    # `group_by_key` and `partition_key` are ordinary query parameters for a
    # read-only metrics tool, so matching it would refuse legitimate
    # registrations. The credential families that *use* the word are matched
    # as whole compounds instead (``privatekey``/``accesskey``/``sshkey``/
    # ``keypair``/``keystore``). A denylist cannot be complete; what actually
    # holds the boundary is that secrets live behind ``credential_ref`` and
    # never in a model-proposable parameter at all.
    #
    # Tokenized twice, and case-folded first: ``_WORDS`` continues a token
    # only through lowercase characters, so tokenizing the original spelling
    # split ``AUTH`` into four single letters and let ``AUTH``/``AUTH_HEADER``/
    # ``COOKIE``/``SESSION``/``SIG`` through while their lowercase forms were
    # refused (bot review finding). The camelCase pass is kept as well, so
    # ``refreshAuth`` still matches while ``author_filter`` still does not.
    camel = [word.lower() for word in _WORDS.findall(name)]
    split = [part for part in re.split(r"[^a-z0-9]+", name.lower()) if part]
    if (set(camel) | set(split)) & _AUTHENTICATION_TOKENS:
        return True
    return any(
        word == "session"
        and (index == len(seq) - 1 or seq[index + 1] in _SESSION_CREDENTIAL_NEXT)
        for seq in (camel, split)
        for index, word in enumerate(seq)
    )


# Prose redaction for free text that is about to become model input (human
# follow-up/correction/event text). Same rule set as the parameter-name
# refusals above -- one definition of "what names a credential" -- applied
# to the three shapes credential material takes in prose: ``name: value`` /
# ``name=value`` where the name is an authentication name, a ``Bearer``/
# ``Basic`` scheme followed by its token, and userinfo in a URL authority
# (the form ToolRegistration refuses in an endpoint). The value is replaced
# by a fixed marker and the surrounding text is kept, so the operator's note
# still reads and the output is deterministic (a rebuild reproduces it).
# Known limit, same as everywhere in this module: a bare secret string with
# no naming context is not decidable here. A URL authority still spelled
# with JSON escapes (``\/\/``, ``\u002f\u002f``, mixed) is covered; userinfo that
# itself holds an escaped quote (``\"``, not a valid URL) is not, and other
# encodings (``%2F``, double escapes, HTML entities) are out of scope (#359).
REDACTED_CREDENTIAL = "[REDACTED_CREDENTIAL]"
#
# Linear-time by construction: a name is only tried at a token start (the
# lookbehind) and is matched possessively, so a very long token (an 8 KiB
# note, a 512 KiB pathological input) is scanned once, never re-scanned per
# start position (an earlier draft backtracked quadratically and hung the
# oversized-input test). A value stops at ``?``/``#`` as well as at
# separators, so a URL's scheme (``https:``) does not swallow the query
# string and ``?api_key=…`` is examined on its own.
#
# The regex only finds the assignment and the value's first character (so a
# search never scans a value it then re-examines); a credential's value is
# then read by ``_value_end`` (PR #195 bot P1): an object or array
# (``password: {...}``,
# ``token = [ "a", "b" ]``) runs to its balancing bracket and a quote met
# inside the value to its closing quote, each to the end of the text when it
# never closes, so no part of the value is left behind the marker. A
# non-credential name's value is examined again from its start, so an
# assignment nested in it (``x=api_key=…``) is still found.
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<name>[A-Za-z][A-Za-z0-9_.\-]*+)(?P<sep>\s*[:=]\s*)"
    r"(?P<value>(?P<scheme>(?:bearer|basic|digest|token)\s+)?[^\s&;,?#\"'])",
    re.IGNORECASE,
)
#: Where an unquoted value ends at nesting depth zero (with whitespace), and
#: where a JSON scalar or a value after its closing quote or bracket does.
_UNQUOTED_STOPS = frozenset("&;,?#")
_JSON_STOPS = frozenset(",}]")
_CREDENTIAL_SCHEME = re.compile(
    r"\b(?P<scheme>bearer|basic)\s+(?P<value>[A-Za-z0-9._~+/=\-]{8,})", re.IGNORECASE
)
_AUTHORITY_USERINFO = re.compile(
    r"(?P<prefix>//)(?P<userinfo>[^\s/@]++)@(?P<host>[^\s/:?#]+)"
)
# The same authority with each ``/`` still JSON-escaped (``\/`` or
# ``\u002f``/``\u002F``, mixed freely, with or without a scheme), as in text that
# is not JSON (an HTML error page, a cut body) or in JSON nested inside a
# JSON string value (#319). Userinfo and host stop at ``\`` and ``"`` too, so
# a match never crosses into the next JSON string; the output has the same
# shape as the literal rule's: prefix as written, marker, ``@``, host.
_ESCAPED_AUTHORITY_USERINFO = re.compile(
    r"(?P<prefix>(?:\\/|\\u002[fF]){2})(?P<userinfo>[^\s/@\\\"]++)@"
    r"(?P<host>[^\s/:?#\\\"]+)"
)
# The forms the unquoted value above stops at: JSON ``"name":"v"``, YAML
# ``name: "v"``, ``name='v'``, and a JSON value that is an object, an array
# (``"name":{...}``) or, after a quoted name, a bare scalar
# (``"name": 4711``). Only the name, separator and the value's first
# character are matched; the value is read only when the name is an
# authentication name. A quoted value runs to its closing quote (same quote,
# backslash escapes honoured) on the same line; one that does not close on
# its line fails closed and everything after the opening quote is redacted,
# since its end cannot be told from text that follows it (M1-04 step 4
# review P1-1: a value continued on the next line leaked). An object or array
# is replaced whole, as a quoted marker, up to its balancing bracket (strings
# and escapes honoured), or to the end of the text when it never balances. A
# non-credential name consumes nothing past its separator, so an assignment
# nested inside an ordinary value is still examined. Each scan is a single
# forward pass (linear time).
_QUOTED_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<name>[A-Za-z][A-Za-z0-9_.\-]*+)(?P<close>[\"']?)"
    r"(?:(?P<sep>\s*[:=]\s*)(?P<quote>[\"'{\[])"
    r"|(?P<scalar_sep>\s*:\s*)(?P<scalar>[^\s\"'{\[\],}]))"
)
# A quoted key spelled with escapes (``"PASS\u0057ORD":``) is the same key to
# any JSON reader, so its value gets the same redaction once the escapes are
# decoded (M1-04 step 4 recheck P1-1), whatever the key's length. Found from
# its end: every quote followed by a separator is a candidate closing quote,
# and its opening quote is the nearest unescaped same quote before it on the
# same line. That backward walk never goes below the previous candidate of
# the same quote (a key cannot contain one), so each character is walked a
# bounded number of times and the pass stays linear. When the walk reaches
# that previous candidate without a quote between, the candidate itself
# opens the key -- in malformed text this may over-redact, never under.
_KEY_END = re.compile(r"[\"'](?=\s*+[:=])")
_KEY_SEP = re.compile(r"\s*+[:=]\s*+")
_SCALAR_VALUE = re.compile(r"[^\s\"'{\[\],}]++")
_JSON_ESCAPE = re.compile(r"\\(?:u(?P<hex>[0-9A-Fa-f]{4})|(?P<char>.))", re.DOTALL)
_QUOTED_VALUE = {
    '"': re.compile(r'(?:[^"\\\n]|\\.)++"'),
    "'": re.compile(r"(?:[^'\\\n]|\\.)++'"),
}
_CLOSERS = {"{": "}", "[": "]"}
# JSON text that is itself encoded into a JSON string, once or more (a log
# line holding a serialized payload, #710): ``{\"password\": \"v\"}``. Every
# quote of that inner text carries the same odd run of backslashes (1, 3, 7
# ...), which no rule above reads as a quote. A credential name followed by
# such a quote and a separator is an assignment at that depth; its value is
# read by ``_escaped_value_end`` with the quote spelled the same way, so an
# escaped quote one level deeper (a longer run) stays inside the value.
_ESCAPED_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<name>[A-Za-z][A-Za-z0-9_.\-]*+)"
    r"(?P<close>\\(?:\\\\)*+\")(?P<sep>\s*+[:=]\s*+)"
)
_ESCAPED_QUOTE = re.compile(r"\\(?:\\\\)*+\"")


def _credential_name(name: str) -> bool:
    return name.lower() in _AUTHENTICATION_KEYS or _authentication_name(name)


def _session_word(name: str) -> bool:
    """Whether ``session`` is any word of ``name`` -- the rule before #366.
    A Redis key such as ``session:4f1c`` or ``spring:session:sessions:…``
    names a stored web session, whose value stays hidden (#366 keeps
    session values redacted; only session *settings* became visible)."""
    words = {word.lower() for word in _WORDS.findall(name)}
    words.update(part for part in re.split(r"[^a-z0-9]+", name.lower()) if part)
    return "session" in words


# A count, not a credential (#366, B1-U3 a): a name ending in ``tokens``
# whose whole value is a number (``prompt_tokens: 12``, ``max_tokens=4000``)
# keeps its value. Only a bare number counts -- a quoted ``"12"``, a list or
# any other text is still redacted (``access_tokens=abc``) -- and not when the
# rest of the name is itself a credential word (``secret_tokens: 5``).
_COUNT_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")


def _count_name(name: str) -> bool:
    end = len(name)
    while end and not name[end - 1].isalnum():
        end -= 1  # ``prompt_tokens_``, as a key may be cut
    if not name[:end].lower().endswith("tokens"):
        return False
    # The rest of the name, as spelled, by the same credential-name rule:
    # ``auth_tokens``, ``session_tokens``, ``cookieTokens`` stay credentials.
    return not _credential_name(name[: end - len("tokens")])


def _token_count(name: str, value: str) -> bool:
    """Whether ``value``, the whole text a credential rule would replace, is
    a number held by a count-named field."""
    return _COUNT_NUMBER.fullmatch(value) is not None and _count_name(name)


def _count_redaction(
    name: str, text: str, start: int, redaction: tuple[str, int] | None
) -> bool:
    """Whether ``_value_redaction``'s result would replace a bare number of a
    count-named field (a quoted or bracketed value never is one)."""
    return (
        redaction is not None
        and redaction[0] == REDACTED_CREDENTIAL
        and _token_count(name, text[start : redaction[1]])
    )


def _container_end(text: str, start: int) -> int:
    """Index after the object or array opening at ``start``, or ``len(text)``
    when it never balances. JSON strings (either quote) are skipped whole."""
    stack: list[str] = []
    quote: str | None = None
    index = start
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in _CLOSERS:
            stack.append(_CLOSERS[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return index + 1
        index += 1
    return len(text)


def _decoded_name(quoted: str) -> str:
    """A quoted key's text with JSON escapes (``\\uXXXX``, ``\\/`` ...) decoded."""
    return _JSON_ESCAPE.sub(
        lambda m: chr(int(m.group("hex"), 16)) if m.group("hex") else m.group("char"),
        quoted,
    )


def _value_end(text: str, start: int, stops: frozenset[str]) -> int:
    """Where a credential value starting at ``start`` ends: at whitespace or
    one of ``stops`` outside any quote or bracket. A quote runs to its
    closing quote (backslash escapes honoured), a bracket to its balancing
    one; either runs to the end of the text when it never closes (fail
    closed). One forward pass."""
    stack: list[str] = []
    quote: str | None = None
    index = start
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in _CLOSERS:
            stack.append(_CLOSERS[char])
        elif stack:
            if char == stack[-1]:
                stack.pop()
        elif char.isspace() or char in stops:
            return index
        index += 1
    return len(text)


def _tail_end(text: str, end: int) -> int:
    """``end`` of a closed JSON value, extended over anything glued to it
    (``"v"rest``, ``{...}rest``) that is not a separator, so no part of a
    malformed value is left behind the marker."""
    if end >= len(text) or text[end].isspace() or text[end] in _JSON_STOPS:
        return end
    return _value_end(text, end, _JSON_STOPS)


def _escaped(text: str, index: int) -> bool:
    """Whether the character at ``index`` follows an odd run of backslashes."""
    run = 0
    while index - run - 1 >= 0 and text[index - run - 1] == "\\":
        run += 1
    return run % 2 == 1


def _value_redaction(text: str, start: int) -> tuple[str, int] | None:
    """The marker for a credential value starting at ``start`` and where the
    value ends, by the rules of ``_redact_quoted``; ``None`` when there is
    nothing to hide (empty, or already the marker)."""
    if start >= len(text) or text.startswith(REDACTED_CREDENTIAL, start):
        return None
    char = text[start]
    if char in "\"'":
        if text[start + 1 : start + 2] in (char, ""):
            return None
        value = _QUOTED_VALUE[char].match(text, start + 1)
        if value is None:
            return char + REDACTED_CREDENTIAL, len(text)
        return char + REDACTED_CREDENTIAL + char, _tail_end(text, value.end())
    if char in _CLOSERS:
        return f'"{REDACTED_CREDENTIAL}"', _tail_end(text, _container_end(text, start))
    if _SCALAR_VALUE.match(text, start) is None:
        return None
    return REDACTED_CREDENTIAL, _value_end(text, start, _JSON_STOPS)


def _redact_escaped_keys(text: str) -> str:
    if "\\" not in text:
        return text  # nothing is spelled with an escape
    parts: list[str] = []
    pos = 0
    previous: dict[str, int] = {}
    for match in _KEY_END.finditer(text):
        end = match.start()
        quote = text[end]
        if end < pos or _escaped(text, end):
            continue
        floor = max(previous.get(quote, -1), pos - 1)
        previous[quote] = end
        opening = None
        index = end - 1
        while index > floor:
            char = text[index]
            if char == "\n":
                break
            if char == quote and not _escaped(text, index):
                opening = index
                break
            index -= 1
        else:
            # Reached the previous candidate (a quote of this kind) with no
            # quote between: it opens this key. A floor at the end of the
            # last redaction is not a quote that may open anything.
            if floor >= pos and text[floor] == quote:
                opening = floor
        if opening is None:
            continue
        name = text[opening + 1 : end]
        if "\\" not in name or not _credential_name(_decoded_name(name)):
            continue  # unescaped names are ``_redact_quoted``'s
        separator = _KEY_SEP.match(text, end + 1)
        if separator is None:
            continue
        redaction = _value_redaction(text, separator.end())
        if redaction is None or _count_redaction(
            _decoded_name(name), text, separator.end(), redaction
        ):
            continue
        marker, value_end = redaction
        parts.append(text[pos : separator.end()] + marker)
        pos = value_end
    parts.append(text[pos:])
    return "".join(parts)


def _escaped_value_end(text: str, start: int, quote: str) -> int:
    """Index after an escaped-quoted string or an object/array starting at
    ``start``, where ``quote`` (backslashes and ``"``) is how a quote is
    spelled at this depth; ``len(text)`` when it never closes (fail closed).
    One forward pass."""
    depth = len(quote) - 1
    stack: list[str] = []
    inside = False
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            run = index
            while run < len(text) and text[run] == "\\":
                run += 1
            if run < len(text) and text[run] == '"':
                if run - index == depth:
                    inside = not inside
                    if not inside and not stack:
                        return run + 1
                index = run + 1
            else:
                index = run
            continue
        if not inside:
            if char in _CLOSERS:
                stack.append(_CLOSERS[char])
            elif stack and char == stack[-1]:
                stack.pop()
                if not stack:
                    return index + 1
        index += 1
    return len(text)


def _redact_escaped_quotes(text: str) -> str:
    if '\\"' not in text:
        return text  # no escaped quote, no escaped JSON
    parts: list[str] = []
    pos = 0
    for match in _ESCAPED_ASSIGNMENT.finditer(text):
        if match.start() < pos or not _credential_name(match.group("name")):
            continue
        start = match.end()
        if text.startswith(REDACTED_CREDENTIAL, start):
            continue
        quote = _ESCAPED_QUOTE.match(text, start)
        if quote is not None:
            if text.startswith(REDACTED_CREDENTIAL + quote.group(), quote.end()):
                continue
            marker = quote.group() + REDACTED_CREDENTIAL + quote.group()
            end = _escaped_value_end(text, start, quote.group())
        elif text[start : start + 1] in _CLOSERS:
            close = match.group("close")
            marker = close + REDACTED_CREDENTIAL + close
            end = _escaped_value_end(text, start, close)
        elif _SCALAR_VALUE.match(text, start) is not None:
            marker = REDACTED_CREDENTIAL
            end = _value_end(text, start, _JSON_STOPS)
            if _token_count(match.group("name"), text[start:end]):
                continue
        else:
            continue
        parts.append(text[pos:start] + marker)
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def _redact_quoted(text: str) -> str:
    parts: list[str] = []
    pos = 0
    while (match := _QUOTED_ASSIGNMENT.search(text, pos)) is not None:
        scalar = match.group("scalar") is not None
        if scalar and not match.group("close"):
            # ``name: value`` unquoted is the assignment rule's; only a
            # quoted name takes a bare JSON scalar here.
            parts.append(text[pos : match.start("scalar")])
            pos = match.start("scalar")
            continue
        start = match.start("scalar") if scalar else match.start("quote")
        redaction = (
            _value_redaction(text, start)
            if _credential_name(match.group("name"))
            else None
        )
        if _count_redaction(match.group("name"), text, start, redaction):
            redaction = None
        if redaction is None:
            # Ordinary name, or nothing to hide: keep it. A bare scalar is
            # skipped whole, read once here so the scan stays linear (#365);
            # any other value is examined from its first character on.
            end = start + 1
            if scalar:
                # Never None: the scalar group matched one char of this class.
                value = _SCALAR_VALUE.match(text, start)
                end = value.end() if value is not None else match.end()
            parts.append(text[pos:end])
            pos = end
            continue
        marker, end = redaction
        parts.append(text[pos:start] + marker)
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def _redact_assignments(text: str) -> str:
    parts: list[str] = []
    pos = 0
    while (match := _CREDENTIAL_ASSIGNMENT.search(text, pos)) is not None:
        name = match.group("name")
        if not _credential_name(name):
            parts.append(text[pos : match.start("value")])
            pos = match.start("value")
            continue
        body = match.start("value") + len(match.group("scheme") or "")
        end = _value_end(text, body, _UNQUOTED_STOPS)
        if match.group("scheme") is None and _token_count(name, text[body:end]):
            # ``total_tokens=5``: a count, kept (#366).
            parts.append(text[pos:end])
            pos = end
            continue
        parts.append(text[pos : match.start("sep")] + match.group("sep"))
        parts.append(REDACTED_CREDENTIAL)
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def redact_credentials(text: str) -> str:
    """Replace credential-bearing spans in prose with ``REDACTED_CREDENTIAL``."""

    redacted = text
    for rule in (_AUTHORITY_USERINFO, _ESCAPED_AUTHORITY_USERINFO):
        redacted = rule.sub(
            lambda m: f"{m.group('prefix')}{REDACTED_CREDENTIAL}@{m.group('host')}",
            redacted,
        )
    redacted = _redact_assignments(redacted)
    redacted = _redact_escaped_keys(redacted)
    redacted = _redact_escaped_quotes(redacted)
    redacted = _redact_quoted(redacted)
    return _CREDENTIAL_SCHEME.sub(
        lambda m: f"{m.group('scheme')} {REDACTED_CREDENTIAL}", redacted
    )


# Kubernetes reference names that point at a Secret without holding any of
# its bytes (2026-11-22 user decision, SREGym p13): a Pod spec saying which
# Secret and key it reads is the evidence for "references Secret X / key Y,
# which does not exist", and the key-name rule below would otherwise wipe the
# whole reference because its field name contains "secret". Exempted only at
# the exact Kubernetes spelling and position -- (field, parent field) ->
# readable sub-fields:
#
# * ``env[].valueFrom.secretKeyRef.{name,key,optional}``
# * ``envFrom[].secretRef.{name,optional}``
# * ``volumes[].secret.{secretName,optional}``
#
# Inside such a reference every other field is replaced whole (fail closed),
# a readable string still passes ``redact_credentials``, and anything that is
# not a string or a bool is replaced too. Secret objects themselves stay
# unreadable (``kubernetes_core.resolve_kind``, A2-6).
# The exemption keys on field shape and position, not on resource kind.
_SECRET_REFERENCES: dict[tuple[str, str], frozenset[str]] = {
    ("secretKeyRef", "valueFrom"): frozenset({"name", "key", "optional"}),
    ("secretRef", "envFrom"): frozenset({"name", "optional"}),
    ("secret", "volumes"): frozenset({"secretName", "optional"}),
}
# A list of references at the same kind of position (B1-D8, with the #775
# decision): ``spec.imagePullSecrets[].name`` -- which registry Secret a Pod
# pulls with. (field, parent field) -> readable sub-fields of each element;
# an element that is not an object is replaced whole.
_SECRET_REFERENCE_LISTS: dict[tuple[str, str], frozenset[str]] = {
    ("imagePullSecrets", "spec"): frozenset({"name"}),
}
_SECRET_REFERENCE_ITEMS = {
    field: names for (field, _), names in _SECRET_REFERENCE_LISTS.items()
}


def redact_payload(value: object) -> object:
    """Redact JSON string values, object keys and credential-named fields, purely.

    An explicit stack keeps traversal linear and independent of nesting depth.
    Return the original value when unchanged, including its byte representation
    when callers use identity to decide whether to serialize again.
    """
    if isinstance(value, str):
        text = redact_credentials(value)
        return value if text == value else text
    if not isinstance(value, (dict, list, tuple)):
        return value
    # A tuple serializes as a JSON array (``json.dumps``, ``canonical``), so it
    # is walked like a list and comes back as one (bot review).
    result: dict[str, object] | list[object] = {} if isinstance(value, dict) else []
    # Each entry also carries the field the container sits under (a list's
    # items inherit the list's field) and, inside a Secret reference, the
    # sub-fields that stay readable.
    stack: list[
        tuple[
            dict[str, object] | list[object] | tuple[object, ...],
            dict[str, object] | list[object],
            str | None,
            frozenset[str] | None,
        ]
    ] = [(value, result, None, None)]
    suffixes: dict[tuple[int, str], int] = {}
    changed = False
    while stack:
        source, destination, parent, readable = stack.pop()
        items = source.items() if isinstance(source, dict) else enumerate(source)
        for raw_key, item in items:
            # A non-string key is serialized by JSON as its scalar text
            # (``1`` -> ``"1"``), so it is examined and stored as that text.
            key = raw_key if isinstance(raw_key, str) else json.dumps(raw_key)
            changed |= key is not raw_key and isinstance(source, dict)
            reference = (
                _SECRET_REFERENCES.get((key, parent))
                if isinstance(source, dict) and readable is None and parent
                else None
            )
            redacted: object
            if readable is not None:
                # Inside a Secret reference: only the named sub-fields, and
                # only as a string (still prose-redacted) or a bool.
                if key in readable and isinstance(item, (str, bool)):
                    redacted = (
                        redact_credentials(item) if isinstance(item, str) else item
                    )
                elif item is None:
                    redacted = None
                else:
                    redacted = REDACTED_CREDENTIAL
                changed |= redacted != item
            elif reference is not None and isinstance(item, dict):
                child_ref: dict[str, object] = {}
                stack.append((item, child_ref, key, reference))
                redacted = child_ref
            elif (
                isinstance(source, dict)
                and parent is not None
                and (key, parent) in _SECRET_REFERENCE_LISTS
                and isinstance(item, (list, tuple))
            ):
                # Walked element by element below; only reachable here, so a
                # list under this field name anywhere else is still replaced.
                child_refs: list[object] = []
                stack.append((item, child_refs, key, None))
                redacted = child_refs
            elif (
                isinstance(source, (list, tuple)) and parent in _SECRET_REFERENCE_ITEMS
            ):
                if isinstance(item, dict):
                    element: dict[str, object] = {}
                    stack.append(
                        (item, element, parent, _SECRET_REFERENCE_ITEMS[parent])
                    )
                    redacted = element
                else:
                    redacted = None if item is None else REDACTED_CREDENTIAL
                    changed |= redacted != item
            elif (
                isinstance(source, dict)
                # The marker's own word must not make a key a credential
                # name (a second pass would wipe ``https://[REDACTED_
                # CREDENTIAL]@host``'s value), but a marker in the source is
                # no exemption either: the rest of the key is still checked
                # (``api_key_[REDACTED_CREDENTIAL]``; independent review).
                and _credential_name(key.replace(REDACTED_CREDENTIAL, ""))
                and item is not None
                and not (
                    # ``"prompt_tokens": 12``: a count, kept (#366).
                    isinstance(item, (int, float))
                    and not isinstance(item, bool)
                    and _count_name(key.replace(REDACTED_CREDENTIAL, ""))
                )
            ):
                # Whatever a credential-named field holds -- a string, a
                # number, an object -- is replaced whole, as the prose rule
                # does for ``"password": 4711`` or ``"token": {...}``.
                redacted = REDACTED_CREDENTIAL
                changed |= redacted != item
            elif isinstance(item, str):
                redacted = redact_credentials(item)
                changed |= redacted != item
            elif isinstance(item, (dict, list, tuple)):
                child: dict[str, object] | list[object] = (
                    {} if isinstance(item, dict) else []
                )
                stack.append(
                    (item, child, key if isinstance(source, dict) else parent, None)
                )
                redacted = child
            else:
                redacted = item
            if isinstance(destination, dict):
                # A key is source text too (a URL with userinfo, a Jaeger
                # service name turned into a count key): redact it, and keep
                # two keys that redact alike apart with a stable suffix
                # rather than letting one overwrite the other.
                name = redact_credentials(key)
                changed |= name != key
                if name in destination:
                    # The next free suffix per base, so many keys that redact
                    # alike cost one probe each, not a rescan (bot review).
                    base = name
                    suffix = suffixes.get((id(destination), base), 1)
                    while name in destination:
                        suffix += 1
                        name = f"{base}#{suffix}"
                    suffixes[(id(destination), base)] = suffix
                    changed = True
                destination[name] = redacted
            else:
                destination.append(redacted)
    return result if changed else value


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise _DuplicateKey
    return result


class _DuplicateKey(ValueError):
    pass


def has_duplicate_keys(text: str) -> bool:
    """Whether a JSON text repeats a key in some object.

    ``json.loads`` keeps the last value of a repeated key, so a payload that
    parses unchanged by redaction may still carry an earlier value -- a
    secret -- in its bytes (``{"password":"s","password":"[REDACTED_
    CREDENTIAL]"}``; independent review). Such bytes are never kept as they
    are. ``False`` for text that is not JSON at all.
    """
    try:
        json.loads(text, object_pairs_hook=_pairs_without_duplicates)
    except _DuplicateKey:
        return True
    except (ValueError, RecursionError):
        return False
    return False


def redact_json_bytes(body: bytes) -> bytes:
    """Redact UTF-8 JSON or prose; preserve unchanged and non-UTF-8 bytes."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        # Not JSON, or nested past the decoder's limit: the text rules still
        # apply, linearly, to the whole body (bot review).
        redacted = redact_credentials(text)
        return body if redacted == text else redacted.encode("utf-8")
    result = redact_payload(payload)
    if result is payload and not has_duplicate_keys(text):
        return body
    # Equivalent to tools.registry.canonical, without its import cycle. A
    # lone surrogate (``"\\ud800"`` in the source) cannot be UTF-8 encoded;
    # such a body keeps its escapes instead of failing the caller (bot review).
    try:
        return json.dumps(
            result, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(result, sort_keys=True, separators=(",", ":")).encode("ascii")
