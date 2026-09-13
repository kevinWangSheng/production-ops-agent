"""Offline wrapper guards for checkout identity and business credential leakage."""

import pytest

from scripts.m0_environment.holmes_baseline import (
    _checkout_code_digest,
    _credential_like,
)


def test_checkout_identity_is_content_digest_and_rejects_empty_or_symlink(tmp_path):
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n")
    first = _checkout_code_digest(source)
    (source / "module.py").write_text("VALUE = 2\n")
    assert _checkout_code_digest(source) != first
    link = tmp_path / "link"
    link.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="upstream checkout invalid"):
        _checkout_code_digest(link)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="upstream checkout empty"):
        _checkout_code_digest(empty)


@pytest.mark.parametrize(
    "value",
    [
        {"Authorization": "Bearer synthetic-token"},
        {"api_key": "synthetic-key"},
        {"nested": [{"x-api-key": "synthetic"}]},
        "Bearer synthetic-token",
    ],
)
def test_credential_like_projection_is_rejected(value):
    assert _credential_like(value) is True


@pytest.mark.parametrize(
    "value",
    [
        {"message": "authorization was denied"},
        {"status": "ok", "data": [{"value": 1}]},
        "ordinary telemetry text",
    ],
)
def test_ordinary_business_projection_has_no_credential_marker(value):
    assert _credential_like(value) is False
