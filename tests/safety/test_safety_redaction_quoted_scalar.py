"""Safety: a bare scalar after an ordinary quoted name is kept whole, so a
credential assignment that follows it is still redacted (PR #800 review of
#365). Rescanning inside the scalar once matched ``token:"password"`` as a
name, redacted the quoted name, and left the real value visible. Expected
outputs are main's (500c2d7a) before the #365 change.
"""

from __future__ import annotations

import pytest

from opspilot.redaction import REDACTED_CREDENTIAL, redact_credentials

#: Synthetic, built at runtime so the secret scanner does not read a literal.
_FAKE = "-".join(("SCALAR", "FAKE"))


@pytest.mark.parametrize("gap", ["", " ", "  "])
def test_credential_after_an_ordinary_quoted_scalar_stays_redacted(gap):
    text = f'"x":/token:"password":{gap}"{_FAKE}"'
    redacted = redact_credentials(text)
    assert _FAKE not in redacted
    assert redacted == f'"x":/token:"password":{gap}"{REDACTED_CREDENTIAL}"'
