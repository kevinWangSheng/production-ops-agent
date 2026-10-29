"""Product-image seam carried forward from the deferred #392 digest test.

B9 wants Compose and the install records to carry ``image@sha256:<digest>``.
The first digest exists only after the user-gated first publication, so in
#394 Compose keeps the local build default and the digest requirement is
enforced by the installer (test_394_installer_contract.py). What is checkable
now: every product service runs the same one image reference, and every
non-product image in Compose is pinned by digest. The remainder is listed in
ISSUE_394_AMBIGUITIES.md.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "compose" / "compose.yaml"
PRODUCT_SERVICES = {"migrate", "init-roles", "web", "worker", "observer", "retention"}
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _services() -> dict[str, Any]:
    assert COMPOSE.is_file(), f"missing Compose file: {COMPOSE.relative_to(REPO)}"
    try:
        import yaml
    except Exception as exc:  # pragma: no cover - environment failure is a test failure
        pytest.fail(f"PyYAML is required to parse the Compose contract: {exc}")
    document = yaml.safe_load(COMPOSE.read_text())
    assert isinstance(document, dict) and isinstance(document.get("services"), dict)
    return document["services"]


def test_product_services_share_one_image_reference_defaulting_to_the_local_build() -> (
    None
):
    services = _services()
    assert PRODUCT_SERVICES <= set(services), sorted(services)
    images = {services[name].get("image") for name in PRODUCT_SERVICES}
    assert len(images) == 1, images
    image = next(iter(images))
    assert isinstance(image, str)
    # Overridable by the installer (which enforces the digest), local by default.
    assert re.fullmatch(r"\$\{OPSPILOT_IMAGE:?-opspilot:local\}", image), image


def test_every_non_product_image_is_digest_pinned() -> None:
    services = _services()
    for name, service in services.items():
        if (
            name in PRODUCT_SERVICES
            or not isinstance(service, dict)
            or "image" not in service
        ):
            continue
        assert DIGEST.search(str(service["image"])), f"{name}: {service['image']}"
