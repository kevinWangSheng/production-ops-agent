"""Pure credential redaction shared by intake, observation and tool registration."""

from __future__ import annotations

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
    {"auth", "cert", "cookie", "p12", "pem", "pfx", "session", "sig"}
)
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
    words = {word.lower() for word in _WORDS.findall(name)}
    words.update(part for part in re.split(r"[^a-z0-9]+", name.lower()) if part)
    return bool(words & _AUTHENTICATION_TOKENS)


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
# no naming context is not decidable here.
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
    r"|(?P<scalar_sep>\s*:\s*)(?P<scalar>[^\s\"'{\[\],}]++))"
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


def _credential_name(name: str) -> bool:
    return name.lower() in _AUTHENTICATION_KEYS or _authentication_name(name)


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
        if redaction is None:
            continue
        marker, value_end = redaction
        parts.append(text[pos : separator.end()] + marker)
        pos = value_end
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
        if redaction is None:
            # Ordinary name, or nothing to hide: keep it and examine the
            # value from its first character on.
            end = match.end() if scalar else start + 1
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
        parts.append(text[pos : match.start("sep")] + match.group("sep"))
        parts.append(REDACTED_CREDENTIAL)
        pos = _value_end(text, body, _UNQUOTED_STOPS)
    parts.append(text[pos:])
    return "".join(parts)


def redact_credentials(text: str) -> str:
    """Replace credential-bearing spans in prose with ``REDACTED_CREDENTIAL``."""

    redacted = _AUTHORITY_USERINFO.sub(
        lambda m: f"{m.group('prefix')}{REDACTED_CREDENTIAL}@{m.group('host')}", text
    )
    redacted = _redact_assignments(redacted)
    redacted = _redact_escaped_keys(redacted)
    redacted = _redact_quoted(redacted)
    return _CREDENTIAL_SCHEME.sub(
        lambda m: f"{m.group('scheme')} {REDACTED_CREDENTIAL}", redacted
    )
