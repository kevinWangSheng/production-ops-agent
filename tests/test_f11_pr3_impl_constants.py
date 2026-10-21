"""The webhook's frozen M agrees with the calibration catalog (F11 PR-3).

Authorization requires the policy's ``min_tracking_seconds`` to equal the
release row's, so a catalog entry with another M would make every
webhook-registered release unauthorizable.
"""

from opspilot.observation.release_policy import load_release_policy_catalog
from opspilot.release_intake import RELEASE_MIN_TRACKING_SECONDS


def test_catalog_policies_use_the_webhook_min_tracking_seconds() -> None:
    catalog = load_release_policy_catalog()
    assert all(
        policy.content.min_tracking_seconds == RELEASE_MIN_TRACKING_SECONDS
        for policy in catalog.policies
    )
