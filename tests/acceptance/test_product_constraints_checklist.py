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


def test_f7_step2_and_step3_acceptance_modules_own_their_indexed_scenarios():
    """F7 PR-B: hostile input (step 2) belongs to ``evidence_context``;
    source secrets (step 3) belong to ``data_flow``. Each acceptance module
    declares the scenario labels it covers; every label must be indexed
    under the section that owns it."""
    from tests.acceptance import (
        test_f7_step2_hostile_input_postgres as step2,
    )
    from tests.acceptance import (
        test_f7_step3_source_secrets as step3,
    )
    from tests.acceptance import (
        test_f7_step3_source_secrets_postgres as step3_pg,
    )

    owners = {
        step2: "evidence_context",
        step3: "data_flow",
        step3_pg: "data_flow",
    }
    for module, section in owners.items():
        labels = module.PRODUCT_CONSTRAINT_SCENARIOS
        assert labels, module.__name__
        for label in labels:
            assert label in PRODUCT_CONSTRAINT_SCENARIO_INDEX[section], (
                module.__name__,
                label,
            )


def test_f7_step1_acceptance_module_owns_the_readonly_scenario():
    """F7 PR-C: entry-point enumeration (step 1) belongs to
    ``explicit_exclusions`` ("F7:readonly")."""
    from tests.acceptance import test_f7_step1_entry_points as step1

    assert step1.PRODUCT_CONSTRAINT_SCENARIOS == ("F7:readonly",)
    for label in step1.PRODUCT_CONSTRAINT_SCENARIOS:
        assert label in PRODUCT_CONSTRAINT_SCENARIO_INDEX["explicit_exclusions"]
