"""Safety: credential redaction stays one forward pass on pathological input
(#365). Whitespace-free ``name:value`` runs used to cost time quadratic in
their length (64 KiB of ``a:b`` took about 2.5 s, 512 KiB about 150 s), so a
large tool result could hold the worker's CPU for minutes before the model
saw it. Each shape here is 512 KiB and must redact in well under the bound.
"""

from __future__ import annotations

import time

import pytest

from opspilot.redaction import redact_credentials

SIZE = 512 * 1024


@pytest.mark.parametrize("chunk", ["a:b", '"a:b"', "/a:b", "ua:b"])
def test_whitespace_free_name_value_runs_redact_in_linear_time(chunk):
    text = chunk * (SIZE // len(chunk))
    started = time.monotonic()
    redact_credentials(text)
    assert time.monotonic() - started < 5, chunk
