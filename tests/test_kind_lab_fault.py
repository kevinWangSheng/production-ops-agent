"""The kind lab's fault hook edits exactly one flag and refuses wrong states."""

import json

import pytest

from scripts.kind_lab import FAULT_VARIANT, FLAG, NORMAL_VARIANT, mutate_flags

FLAGS = {
    "$schema": "https://flagd.dev/schema/v0/flags.json",
    "flags": {
        FLAG: {
            "state": "ENABLED",
            "variants": {"100%": 1, "10%": 0.1, "off": 0},
            "defaultVariant": "off",
        },
        "other": {"state": "ENABLED", "variants": {"on": True}, "defaultVariant": "on"},
    },
}


def test_inject_moves_only_payment_failure() -> None:
    out = json.loads(
        mutate_flags(json.dumps(FLAGS).encode(), FAULT_VARIANT, NORMAL_VARIANT)
    )
    assert out["flags"][FLAG]["defaultVariant"] == "100%"
    assert out["flags"]["other"] == FLAGS["flags"]["other"]
    assert out["flags"][FLAG]["variants"] == FLAGS["flags"][FLAG]["variants"]


def test_restore_round_trips() -> None:
    injected = mutate_flags(json.dumps(FLAGS).encode(), FAULT_VARIANT, NORMAL_VARIANT)
    restored = mutate_flags(injected, NORMAL_VARIANT, FAULT_VARIANT)
    assert json.loads(restored) == FLAGS


def test_repeat_injection_refused() -> None:
    injected = mutate_flags(json.dumps(FLAGS).encode(), FAULT_VARIANT, NORMAL_VARIANT)
    with pytest.raises(SystemExit):
        mutate_flags(injected, FAULT_VARIANT, NORMAL_VARIANT)


def test_unknown_variant_refused() -> None:
    with pytest.raises(SystemExit):
        mutate_flags(json.dumps(FLAGS).encode(), "50%", NORMAL_VARIANT)
