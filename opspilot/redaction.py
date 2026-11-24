"""Pure credential redaction shared by intake, observation and tool registration."""

from __future__ import annotations

import bisect
import contextvars
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
    # Redis/Valkey password directives (#818): ``--requirepass v``,
    # ``masterauth v``; ``primaryauth`` is Valkey's name for ``masterauth``.
    "masterauth",
    "passwd",
    "password",
    "primaryauth",
    "privatekey",
    "publickey",
    "requirepass",
    "secret",
    "signature",
    "sshkey",
    "token",
    "truststore",
)
_AUTHENTICATION_TOKENS = frozenset(
    {"auth", "cert", "cookie", "p12", "pem", "pfx", "sessionid", "sessionids", "sig"}
)
# ``session`` names a credential only as a name's last word or before a word
# that names the session itself (#366, B1-U3 a, narrowed by the lead's
# safe-direction additions): ``session``, ``session_id``, ``sessionKey``,
# ``session_jwt``, ``sessionStore`` stay credentials; ``sessionAffinity``,
# ``SESSION_TIMEOUT``, ``session_timeout_ms``, ``ssoSessionIdleTimeout`` and
# ``stored_session_state`` are configuration or status about sessions.
_SESSION_CREDENTIAL_NEXT = frozenset(
    {
        *("id", "key", "cookie", "jwt", "ticket", "sid", "uuid", "nonce"),
        *("hash", "value", "blob", "string", "handle", "store", "payload"),
        *("ids", "keys", "cookies", "jwts", "tickets", "sids", "uuids"),
        *("nonces", "hashes", "values", "blobs", "strings", "handles"),
        *("stores", "payloads", "data", "bytes"),
    }
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
# with JSON escapes (``\/\/``, ``\u002f\u002f``, mixed) is covered, and so
# are three more encodings of ``//`` (#359): percent (``%2F%2F``, ``@`` also
# as ``%40``), any deeper backslash escaping (``\\\/\\\/``) and HTML
# entities (``&#x2F;``, ``&#47;``, ``&sol;``; ``@`` also as ``&#64;``,
# ``&#x40;``, ``&commat;``). Each needs the encoded ``//`` first, so a ``%40``
# in a query string or an ``&#x2F;`` path is left alone. Not recognised:
# userinfo that itself holds an escaped quote (``\"``, not a valid URL), a
# ``//`` mixing encodings (``%2F/``, ``\\\/\/``), double percent-encoding
# (``%252F``) and base64.
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
# The same authority encoded three more ways (#359), one rule each, each
# anchored on its own encoded ``//``. Output: prefix as written, marker,
# the ``@`` as written (``%40``, ``&#64;``, ``&#x40;``, ``&commat;`` or ``@``),
# host. Userinfo and host stop at the
# encoding's own spelling of ``/``, ``?``, ``#`` and ``@`` (and at ``\``,
# ``"``, ``<``, ``>``, ``'`` and whitespace), so a match never runs past the
# authority; each part is possessive, so a scan is a single forward pass.
#
# * percent: ``%2F%2Fuser%3Apw%40host`` (``https%3A%2F%2F...``, a URL inside
#   a query string or a log of one);
_PERCENT_AUTHORITY_USERINFO = re.compile(
    r"(?P<prefix>(?:%2[fF]){2})"
    r"(?P<userinfo>(?:[^\s/@%?#&\\\"<>']|%(?!2[fF]|3[fF]|23|40)[0-9A-Fa-f]{2})++)"
    r"(?P<at>@|%40)(?P<host>[^\s/:?#%&\\\"<>']+)"
)
# * backslashes: ``//`` escaped two or more levels deep (``\\\/`` is JSON
#   in a JSON string in a JSON string; ``\\\\u002f`` likewise). Tried only
#   where a backslash run starts, so a long run is scanned once;
_DEEP_ESCAPED_AUTHORITY_USERINFO = re.compile(
    r"(?<!\\)(?P<prefix>(?:\\{2,}+(?:/|u002[fF])){2})(?P<userinfo>[^\s/@\\\"]++)@"
    r"(?P<host>[^\s/:?#\\\"]+)"
)
# * HTML entities: ``&#x2F;``/``&#47;``/``&sol;`` (``@`` also ``&#64;``,
#   ``&#x40;``, ``&commat;``), as in an HTML error page.
_ENTITY_SLASH = r"&#(?:[xX]0*2[fF]|0*47);|&sol;"
_ENTITY_AUTHORITY_USERINFO = re.compile(
    r"(?P<prefix>(?:" + _ENTITY_SLASH + r"){2})"
    r"(?P<userinfo>(?:[^\s/@&?#<>\"'\\]"
    r"|&(?!#(?:[xX]0*2[fF]|0*47|[xX]0*40|0*64|[xX]0*3[fF]|0*63|[xX]0*23|0*35);"
    r"|sol;|commat;|quest;|num;)[#A-Za-z0-9]{1,8};)++)"
    r"(?P<at>@|&#(?:[xX]0*40|0*64);|&commat;)(?P<host>[^\s/:?#&<>\"'\\]+)"
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


# An environment variable is a credential by the same name rule, and on env
# names only also by the words ``pass``/``pwd`` (#574, B1-U1 a, B1-D4):
# ``DB_PASS``, ``RABBITMQ_DEFAULT_PASS``, ``SMTP_PWD``. Not in the general
# rule, where ``golden_pass: 3/3`` would be hidden. ``DB_HOST``, ``LOG_LEVEL``
# and ``SESSION_TIMEOUT`` stay visible. An env value is a string, so the
# ``*_tokens`` count exemption does not apply (``MAX_TOKENS="4096"`` is
# hidden, B1-D6).
_ENV_CREDENTIAL_WORDS = frozenset({"pass", "pwd"})


def _env_credential_name(name: str) -> bool:
    if _credential_name(name):
        return True
    words = {word.lower() for word in _WORDS.findall(name)}
    words.update(part for part in re.split(r"[^a-z0-9]+", name.lower()) if part)
    return bool(words & _ENV_CREDENTIAL_WORDS)


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
_CODE_WORDS = frozenset({"otp", "pin", "mfa", "passcode", "refresh"})
_COUNT_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")


def _count_name(name: str) -> bool:
    end = len(name)
    while end and not name[end - 1].isalnum():
        end -= 1  # ``prompt_tokens_``, as a key may be cut
    if not name[:end].lower().endswith("tokens"):
        return False
    # The rest of the name, as spelled, by the same credential-name rule:
    # ``auth_tokens``, ``session_tokens``, ``cookieTokens`` stay credentials;
    # so do one-time and refresh codes (``otp_tokens``, ``refreshTokens``).
    rest = name[: end - len("tokens")]
    words = {word.lower() for word in _WORDS.findall(rest)}
    words.update(part for part in re.split(r"[^a-z0-9]+", rest.lower()) if part)
    return not (_credential_name(rest) or words & _CODE_WORDS)


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


# A credential pair glued to a credential value (#803, R2-1: fail closed).
# A value read up to whitespace or a stop can end early, with the next
# key's name as its own last word: ``"password":x&"secret": "v"`` reads
# ``x&"secret":``, ``password=x|token :v`` reads ``x|token``, ``token:
# "password": "v"`` reads ``"password":``. The rules above are left exactly
# as they read; a rule whose hidden span ends that way puts a mark (a
# private-use character absent from the text, ``_GLUE_MARK``) right after
# its marker, and ``_redact_glued`` hides the value after each mark once all
# rules have run, then strips the marks. It only replaces more of the rules'
# output, so it never shows what they hid.
_GLUE_MARK: contextvars.ContextVar[str] = contextvars.ContextVar(
    "opspilot_glue_mark", default=""
)
#: Characters of a name read backwards from a separator (``_glued_value``);
#: the quotes and backslashes a quoted or escaped key may end with.
_NAME_CHARS = frozenset("_.-")
_KEY_QUOTES = frozenset("\"'`\\")
_GLUED_SCHEMES = ("bearer", "basic", "digest", "token")
#: Where a glued value ends (with whitespace): a stop of every rule, so the
#: read is at least as long as any rule's own read of that pair, and a
#: second call, reading the marker by any rule, ends it at the same place.
_GLUED_STOPS = frozenset(",")


def _credential_before(text: str, start: int, separator: int) -> bool:
    """Whether the name just before ``separator``, read backwards no
    further than ``start`` (closing quotes and backslashes skipped), is a
    credential name. Reads only that name."""
    name_end = separator
    while name_end > start and text[name_end - 1] in " \t":
        name_end -= 1
    while name_end > start and text[name_end - 1] in _KEY_QUOTES:
        name_end -= 1
    name_start = name_end
    while name_start > start and (
        text[name_start - 1].isalnum() or text[name_start - 1] in _NAME_CHARS
    ):
        name_start -= 1
    return name_start < name_end and _credential_name(text[name_start:name_end])


def _glued_value(text: str, start: int, end: int) -> bool:
    """Whether the hidden value span ``[start, end)`` ends with a credential
    name and its separator inside it (``x&"secret":``), with the name while
    the separator follows past blanks (``x|token`` before `` :v``), or with
    a credential name, its separator and an auth scheme word
    (``x|authorization=Bearer`` before `` v``)."""
    index = end
    while index < len(text) and text[index] in " \t":
        index += 1
    if index < len(text) and text[index] in ":=":
        return _credential_before(text, start, end)
    if end > start and text[end - 1] in ":=":
        return _credential_before(text, start, end - 1)
    tail = text[max(start, end - 6) : end].lower()
    for word in _GLUED_SCHEMES:
        begin = end - len(word)
        if begin > start and tail.endswith(word):
            index = begin
            while index > start and text[index - 1] in " \t":
                index -= 1
            if index > start and text[index - 1] in ":=":
                return _credential_before(text, start, index - 1)
    return False


def _ends_in_scheme_word(text: str, start: int, end: int) -> bool:
    """Whether ``[start, end)`` ends in a whole auth scheme word
    (``x|bearer``), whose token is the next word."""
    tail = text[max(start, end - 6) : end].lower()
    for word in _GLUED_SCHEMES:
        begin = end - len(word)
        if begin >= start and tail.endswith(word):
            return begin == start or not (
                text[begin - 1].isalnum() or text[begin - 1] == "_"
            )
    return False


def _glue_mark(text: str, start: int, end: int) -> str:
    """The mark to put after the marker that replaces ``[start, end)``: when
    a credential pair is glued to that span, or the span itself ended in a
    mark (the value it marked still follows)."""
    mark = _GLUE_MARK.get()
    if not mark or end <= start:
        return ""
    if text[end - 1] == mark or _glued_value(text, start, end):
        return mark
    return ""


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
        parts.append(
            text[pos : separator.end()]
            + marker
            + _glue_mark(text, separator.end(), value_end)
        )
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
        parts.append(text[pos:start] + marker + _glue_mark(text, start, end))
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
        parts.append(text[pos:start] + marker + _glue_mark(text, start, end))
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
        parts.append(REDACTED_CREDENTIAL + _glue_mark(text, body, end))
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


# A credential-named command-line flag's value (#802, R2-1). The flag's name
# is judged by the shared credential-name rule; one or two leading dashes.
# Its value is hidden in each form it takes:
#
# * joined: ``--password=v``, ``-token=v``, ``--password: v``;
# * the next token: ``--password v``, across blanks, newlines, blank lines,
#   a ``\`` continuation, a ``# comment`` and a YAML bullet (``- --password``
#   / ``- v``, kubectl ``describe`` text with the value on the next line);
# * the next element of a list written as text: JSON or Python
#   (``"--password","v"``, also escaped once or more inside a JSON string,
#   as in a ``last-applied-configuration`` annotation) or a YAML flow list
#   (``[--password, v]``).
#
# The next token is hidden unless it starts with ``-`` (another flag); fail
# closed otherwise, so an ordinary word after such a flag -- even the next
# log line -- is occasionally hidden too. A bare number under a ``*tokens``
# name stays (``--max-tokens 4000``, #366). A quoted value is hidden whole.
# An unquoted one is a shell word: it ends at whitespace, inside a list also
# at ``,``/``]``/``}``, and at a quote that closes the string the flag sits
# in (one followed by the end, whitespace or ``,]}:);``; a backtick by any
# character but a letter, digit or ``_``); any other quote runs to its
# match, or to the end of the text (fail closed). In a list, a
# value starting with ``]``/``}``/``)`` is the list's end. An auth scheme
# word (``--token Bearer x``) takes the token after it along.
#
# This rule runs after every other rule (``redact_credentials``) and only
# replaces more of their output, so it never shows what they hid. A flag
# inside a ``# comment`` that another flag's separator skipped is still
# examined. Whether a flag is a list element is read from the text before
# it, which a marker may have replaced; a marker there counts as a possible
# list for the value (so a second call reads the value as the first did). Only the
# flag is matched by a pattern; the separator and value are each read once
# after it, so a scan is a single forward pass. Not recognised: a flag glued
# to a preceding letter, digit, ``_``/``.``/``-`` or ``}``/``)``/``]``
# (``{{ x }}-credentials``), a ``/password`` or Unicode-dash flag, and a
# credential-flag value that itself starts with ``-``.
_FLAG_START = re.compile(
    r"(?<![A-Za-z0-9_.\-})\]])-{1,2}+(?P<name>[A-Za-z][A-Za-z0-9_.\-]*+)"
)
#: A quote as spelled at some JSON escaping depth (``"``, ``\"``, ``\\\"``),
#: or a backtick.
_FLAG_QUOTE = re.compile(r"\\*+[\"'`]")
_FLAG_JOIN = re.compile(r"[ \t]*+[=:][ \t]*+")
_FLAG_BLANKS = re.compile(r"[ \t]++")
_FLAG_SCHEMES = frozenset({"bearer", "basic", "digest", "token"})
#: Where a quote met in an unquoted value closes the enclosing string.
_ENCLOSING_END = frozenset(",]}:);")
_LIST_STOPS = frozenset(",]}")
#: One element of a command list that is a whole flag (``--password``,
#: ``-token``, ``--password=`` with nothing after it).
_FLAG_ELEMENT = re.compile(r"\s*+-{1,2}+(?P<name>[A-Za-z][A-Za-z0-9_.\-]*+)=?\s*+")


class _NextNewline:
    """The next ``\\n`` at or after an index, remembered so that many flags
    on one long line find its end once (linear time)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._from = 0
        self._found = -1

    def at_or_after(self, index: int) -> int:
        if not self._from <= index <= self._found:
            found = self._text.find("\n", index)
            self._from = index
            self._found = len(self._text) if found == -1 else found
        return self._found


def _in_list(text: str, start: int) -> tuple[bool, bool]:
    """Whether the flag starting at ``start`` is a list element -- before it
    (and its opening quote), past whitespace, comes ``[``, ``,`` or a YAML
    bullet -- and whether it may be one: it also follows a marker, which may
    have replaced the ``,`` or ``[`` (a second call then reads the value as
    the first did, so the result is stable)."""
    index = start - 1
    if index >= 0 and text[index] in "\"'`":
        index -= 1
        while index >= 0 and text[index] == "\\":
            index -= 1
    while index >= 0 and text[index].isspace():
        index -= 1
    if index < 0:
        return False, False
    if text[index] in "[," or (
        text[index] == "-" and (index == 0 or text[index - 1].isspace())
    ):
        return True, True
    marker_end = index + 1
    while marker_end > 0 and text[marker_end - 1] in "\"'`\\":
        marker_end -= 1
    return False, text.endswith(REDACTED_CREDENTIAL, 0, marker_end)


def _flag_separator(
    text: str,
    index: int,
    comma: bool,
    newline: _NextNewline,
    after_comment: dict[tuple[int, bool], int | None],
) -> int | None:
    """Where the value after a flag starts: past whitespace (newlines and
    blank lines too), a ``\\`` continuation, one ``,`` when the flag is a
    list element, a ``# comment`` and a YAML bullet. ``None`` when nothing
    separates the flag from what follows, or the text ends.

    Every flag inside one comment line resumes at that line's end in the
    same state, so the walk from there is done once and remembered in
    ``after_comment`` (keyed by the newline and whether a ``,`` may still be
    taken); without it many flags in a comment re-walk a long run of blank
    lines after it (quadratic, review of #817)."""
    resumed: list[tuple[int, bool]] = []
    result = _walk_separator(text, index, comma, newline, after_comment, resumed)
    for key in resumed:
        after_comment[key] = result
    return result


def _walk_separator(
    text: str,
    index: int,
    comma: bool,
    newline: _NextNewline,
    after_comment: dict[tuple[int, bool], int | None],
    resumed: list[tuple[int, bool]],
) -> int | None:
    crossed = False
    line_start = False
    seen_comma = False
    end = len(text)
    while index < end:
        char = text[index]
        if char.isspace():
            line_start = line_start or char == "\n"
            crossed = True
            index += 1
        elif char == "\\" and text.startswith("\n", index + 1):
            crossed = True
            index += 2
        elif char == "\\" and text.startswith("\r\n", index + 1):
            crossed = True
            index += 3
        elif char == "," and comma and not seen_comma:
            seen_comma = crossed = True
            index += 1
        elif char == "#" and crossed:
            index = newline.at_or_after(index)
            key = (index, comma and not seen_comma)
            if key in after_comment:
                return after_comment[key]
            resumed.append(key)
        elif char == "-" and line_start and text[index + 1 : index + 2] in (" ", "\t"):
            line_start = False
            index += 1
        else:
            break
    return index if crossed and index < end else None


def _encloses(text: str, index: int, quote: str) -> bool:
    """Whether ``quote`` just before ``index`` closes the enclosing string:
    the end, whitespace or ``,]}:);`` follow it -- for a backtick (a Markdown
    code span) any character but a letter, digit or ``_``."""
    if index >= len(text) or text[index].isspace() or text[index] in _ENCLOSING_END:
        return True
    return quote == "`" and not (text[index].isalnum() or text[index] == "_")


def _flag_word_end(text: str, start: int, in_list: bool) -> int:
    """Where an unquoted flag value starting at ``start`` ends (see above)."""
    index = start
    end = len(text)
    while index < end:
        char = text[index]
        if char.isspace() or (in_list and char in _LIST_STOPS):
            return index
        if char == "\\" and text[index + 1 : index + 2] in ('"', "'", "`"):
            if _encloses(text, index + 2, text[index + 1]):
                return index
            index += 2
            continue
        if char in "\"'`":
            if _encloses(text, index + 1, char):
                return index
            closing = text.find(char, index + 1)
            if closing == -1:
                return end
            index = closing + 1
            continue
        index += 1
    return end


def _flag_value(
    text: str, name: str, start: int, in_list: bool, joined: bool
) -> tuple[str, int] | None:
    """The marker for credential flag ``name``'s value starting at
    ``start`` and where the value ends; ``None`` for another flag, the end
    of a list, a count, or nothing to hide. A ``joined`` value (after ``=``
    or ``:``) is the value even when it starts with ``-``."""
    if start >= len(text) or text.startswith(REDACTED_CREDENTIAL, start):
        return None
    char = text[start]
    if not joined and (char == "-" or (in_list and char in "]})")):
        return None
    quote = _FLAG_QUOTE.match(text, start)
    if quote is not None:
        spelled = quote.group()
        after = quote.end()
        if text.startswith(("-", spelled), after) or text.startswith(
            REDACTED_CREDENTIAL + spelled, after
        ):
            return None  # a quoted flag, an empty string, or already hidden
        if spelled in ('"', "'"):
            return _value_redaction(text, start)
        if spelled == "`":
            closing = text.find("`", after)
            end = len(text) if closing == -1 else closing + 1
            return f"`{REDACTED_CREDENTIAL}`", end
        if spelled.endswith('"'):
            return (
                spelled + REDACTED_CREDENTIAL + spelled,
                _escaped_value_end(text, start, spelled),
            )
    if char in _CLOSERS:
        return _value_redaction(text, start)
    end = _flag_word_end(text, start, in_list)
    if end == start or _token_count(name, text[start:end]):
        return None
    if text[start:end].lower() in _FLAG_SCHEMES:
        blanks = _FLAG_BLANKS.match(text, end)
        if blanks is not None:
            scheme_end = _flag_word_end(text, blanks.end(), in_list)
            if scheme_end > blanks.end():
                end = scheme_end
    return REDACTED_CREDENTIAL, end


def _redact_flag_values(text: str) -> str:
    if "-" not in text:
        return text
    # Value spans by start. A flag inside a ``# comment`` that an earlier
    # flag's separator skipped is still examined, so its span may start
    # before one already found.
    spans: dict[int, tuple[str, int]] = {}
    starts: list[int] = []
    newline = _NextNewline(text)
    after_comment: dict[tuple[int, bool], int | None] = {}
    for match in _FLAG_START.finditer(text):
        if not _credential_name(match.group("name")):
            continue
        covered = bisect.bisect_right(starts, match.start()) - 1
        if covered >= 0 and match.start() < spans[starts[covered]][1]:
            continue  # inside a value already hidden
        name = match.group("name")
        in_list, list_value = _in_list(text, match.start())
        index = match.end()
        close = _FLAG_QUOTE.match(text, index)
        join = None if close is not None else _FLAG_JOIN.match(text, index)
        if join is not None:
            start: int | None = join.end()
        else:
            index = close.end() if close is not None else index
            start = _flag_separator(
                text, index, close is not None or in_list, newline, after_comment
            )
        if start is None or start in spans:
            continue
        redaction = _flag_value(
            text, name, start, list_value or close is not None, join is not None
        )
        if redaction is None:
            continue
        spans[start] = redaction
        bisect.insort(starts, start)
    if not spans:
        return text
    parts: list[str] = []
    pos = 0
    for start in starts:
        marker, end = spans[start]
        if start < pos:
            if end > pos:  # overlaps the span before: hide the rest too
                parts.append(REDACTED_CREDENTIAL + _glue_mark(text, pos, end))
                pos = end
            continue
        parts.append(text[pos:start] + marker + _glue_mark(text, start, end))
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


# A Redis/Valkey password directive written as a config line (#818):
# ``requirepass v`` / ``masterauth v`` / ``primaryauth v`` in a redis.conf
# ConfigMap, a log or ``CONFIG SET`` text. The directive word stands alone
# (not glued to a preceding letter, digit, ``_``/``.``/``-``; a JSON-escaped
# ``\n``/``\t`` before it counts as a line break) and is followed by blanks
# on the same line; the value is then read like a joined flag value (#802):
# a quoted value whole, otherwise up to whitespace, also when it starts
# with ``-``. Fail closed: the word after ``requirepass`` in ordinary prose
# is hidden too. The ``--requirepass v`` and ``requirepass=v`` forms are the
# flag and assignment rules' (the names are credential names). Runs with
# the flag rule, after every other rule, and only replaces more.
_DIRECTIVE = re.compile(
    r"(?:(?<![A-Za-z0-9_.\-])|(?<=\\[nt]))"
    r"(?P<name>requirepass|masterauth|primaryauth)[ \t]++",
    re.IGNORECASE,
)


def _redact_directives(text: str) -> str:
    parts: list[str] = []
    pos = 0
    for match in _DIRECTIVE.finditer(text):
        start = match.end()
        if start < pos:
            continue  # inside a value already hidden
        redaction = _flag_value(text, match.group("name"), start, False, True)
        if redaction is None:
            continue
        marker, end = redaction
        parts.append(text[pos:start] + marker + _glue_mark(text, start, end))
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def _credential_flag(item: object) -> str | None:
    """The name of ``item`` when it is a whole credential-named flag, one
    element of a command list."""
    if not isinstance(item, str):
        return None
    flag = _FLAG_ELEMENT.fullmatch(item)
    if flag is None or not _credential_name(flag.group("name")):
        return None
    return flag.group("name")


def _flag_value_indices(items: list[object] | tuple[object, ...]) -> set[int]:
    """Indices of the list elements that are a credential flag's value (#802,
    R2-1): the element right after a whole credential flag
    (``["--password", "v"]``), unless it is another flag (a string starting
    with ``-``), ``None``, empty, or a number under a ``*tokens`` name."""
    hidden: set[int] = set()
    for index in range(len(items) - 1):
        name = _credential_flag(items[index])
        if name is None:
            continue
        value = items[index + 1]
        if value is None or value == "" or value == REDACTED_CREDENTIAL:
            continue
        if isinstance(value, str) and (
            value.startswith("-") or _token_count(name, value)
        ):
            continue
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and _count_name(name)
        ):
            continue
        hidden.add(index + 1)
    return hidden


_ENCODED_AUTHORITY_USERINFO = (
    _DEEP_ESCAPED_AUTHORITY_USERINFO,
    _PERCENT_AUTHORITY_USERINFO,
    _ENTITY_AUTHORITY_USERINFO,
)


def _redact_userinfo(text: str, *rules: re.Pattern[str]) -> str:
    for rule in rules:
        text = rule.sub(
            lambda m: (
                f"{m.group('prefix')}{REDACTED_CREDENTIAL}"
                f"{m.groupdict().get('at') or '@'}{m.group('host')}"
            ),
            text,
        )
    return text


# A Kubernetes env entry written as JSON text (#574, B1-D5): the
# ``last-applied-configuration`` annotation, a tabular cell, a log line --
# ``"name":"DB_PASSWORD","value":"..."``, name first as canonical JSON (keys
# sorted) and kubectl write it. The value is read by ``_value_redaction``.
# Not recognised: ``value`` before ``name``, other keys between them, and
# JSON escaped one level deeper (``\"name\":...``).
_ENV_PAIR = re.compile(
    r'"name"\s*+:\s*+"(?P<env>[A-Za-z_][A-Za-z0-9_.\-]{0,255}+)"'
    r'\s*+,\s*+"value"\s*+:\s*+'
)


def _redact_env_pairs(text: str) -> str:
    if '"value"' not in text:
        return text
    parts: list[str] = []
    pos = 0
    for match in _ENV_PAIR.finditer(text):
        if match.start() < pos or not _env_credential_name(match.group("env")):
            continue
        redaction = _value_redaction(text, match.end())
        if redaction is None:
            continue
        marker, end = redaction
        parts.append(
            text[pos : match.end()] + marker + _glue_mark(text, match.end(), end)
        )
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def _redact_prose(text: str) -> str:
    redacted = _redact_name_rules(text)
    # The env pairs run after the rules above, so whatever those hid stays
    # hidden (a value span read here can be shorter than theirs); when they
    # changed anything, the rules above run once more on the result, so a
    # second call finds nothing new.
    paired = _redact_env_pairs(redacted)
    return redacted if paired == redacted else _redact_name_rules(paired)


def _redact_name_rules(text: str) -> str:
    redacted = _redact_userinfo(text, _AUTHORITY_USERINFO, _ESCAPED_AUTHORITY_USERINFO)
    redacted = _redact_assignments(redacted)
    redacted = _redact_escaped_keys(redacted)
    redacted = _redact_escaped_quotes(redacted)
    redacted = _redact_quoted(redacted)
    return _CREDENTIAL_SCHEME.sub(
        lambda m: f"{m.group('scheme')} {REDACTED_CREDENTIAL}", redacted
    )


def _redact_glued(text: str, mark: str) -> str:
    """Hide the value after each glue mark (``_glue_mark``) and strip the
    marks. The value starts past blanks, one separator (when the span ended
    before it) and whitespace; a quote or bracket is read by
    ``_value_redaction``'s rules, then, like any other value, up to
    whitespace or ``,``; an auth scheme word takes the word after it. A
    value that itself ends with a glued credential pair continues the run.
    One forward pass."""
    parts: list[str] = []
    pos = 0
    size = len(text)
    index = text.find(mark)
    while index != -1:
        if index < pos:
            index = text.find(mark, pos)
            continue
        at = index + 1
        while True:
            start = at
            while start < size and text[start] in " \t":
                start += 1
            if start < size and text[start] in ":=":
                start += 1
            while start < size and text[start].isspace():
                start += 1
            if start >= size or text.startswith(REDACTED_CREDENTIAL, start):
                break
            if text[start] == mark:
                break  # the next mark's own value follows
            marker = REDACTED_CREDENTIAL
            if text[start] in "\"'" or text[start] in _CLOSERS:
                redaction = _value_redaction(text, start)
                if redaction is None:
                    break
                marker, end = redaction
                end = _value_end(text, end, _GLUED_STOPS)
            else:
                end = _value_end(text, start, _GLUED_STOPS)
                if end == start:
                    break
                if _ends_in_scheme_word(text, start, end):
                    word = end
                    while word < size and text[word] in " \t":
                        word += 1
                    after = _value_end(text, word, _GLUED_STOPS)
                    if after > word:
                        end = after
            parts.append(text[pos:start] + marker)
            pos = at = end
            if not (text[end - 1] == mark or _glued_value(text, start, end)):
                break
        index = text.find(mark, max(pos, index + 1))
    parts.append(text[pos:])
    return "".join(parts).replace(mark, "")


def _free_mark(text: str) -> str:
    """A private-use character absent from ``text``, or ``""``."""
    if "\ue000" not in text:
        return "\ue000"
    present = set(text)
    for code in range(0xE001, 0xF900):
        if chr(code) not in present:
            return chr(code)
    return ""


def redact_credentials(text: str) -> str:
    """Replace credential-bearing spans in prose with ``REDACTED_CREDENTIAL``."""
    mark = _free_mark(text)
    token = _GLUE_MARK.set(mark)
    try:
        redacted = _redact_rules(text)
        if not mark or mark not in redacted:
            return redacted
        glued = _redact_glued(redacted, mark)
        if glued == redacted.replace(mark, ""):
            return glued
        # The glued values hidden, every rule runs once more on the result
        # (a hidden value changes what they read around it).
        again = _redact_rules(glued)
        return _redact_glued(again, mark) if mark in again else again
    finally:
        _GLUE_MARK.reset(token)


def _redact_rules(text: str) -> str:
    # The encoded forms (#359) run after the rules above, so whatever those
    # hid stays hidden and these only replace userinfo still in view. Their
    # output keeps ``@`` as it was spelled (``%40``, ``&#64;``, ...), so it
    # never completes a literal authority to its left or exposes a name.
    redacted = _redact_userinfo(_redact_prose(text), *_ENCODED_AUTHORITY_USERINFO)
    # Flag values last (#802): they only hide more of the result. A hidden
    # value changes what the rules read around it (a key's quotes, a list's
    # commas), so when it hid anything all rules run once more on the result.
    flagged = _redact_directives(_redact_flag_values(redacted))
    if flagged == redacted:
        return redacted
    again = _redact_userinfo(_redact_prose(flagged), *_ENCODED_AUTHORITY_USERINFO)
    return _redact_directives(_redact_flag_values(again))


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
        # A command list's element after a credential flag is its value
        # (``args: ["--password", "v"]``, #802).
        flag_values = (
            _flag_value_indices(source) if isinstance(source, (list, tuple)) else ()
        )
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
            if flag_values and raw_key in flag_values:
                redacted = REDACTED_CREDENTIAL
                changed |= redacted != item
            elif (
                readable is None
                and parent == "env"
                and key == "value"
                and isinstance(source, dict)
                and isinstance(env_name := source.get("name"), str)
                and _env_credential_name(env_name)
                and item is not None
            ):
                # ``env[].value`` of a credential-named variable (#574).
                redacted = REDACTED_CREDENTIAL
                changed |= redacted != item
            elif readable is not None:
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
