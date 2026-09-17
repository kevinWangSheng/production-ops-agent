from opspilot.acceptance import PRODUCT_CONSTRAINT_SCENARIO_INDEX


def test_every_product_constraints_section_has_a_deterministic_acceptance_owner():
    assert set(PRODUCT_CONSTRAINT_SCENARIO_INDEX) == {
        "explicit_exclusions",
        "workflow_uncertainty_visible",
        "evidence_context",
        "runtime_human_control",
        "recovery_observations",
        "data_flow",
    }
    assert all(entries for entries in PRODUCT_CONSTRAINT_SCENARIO_INDEX.values())


def test_recovery_is_explicitly_not_claimed_by_this_investigation_harness():
    assert PRODUCT_CONSTRAINT_SCENARIO_INDEX["recovery_observations"] == (
        "F6:not_implemented",
    )
