"""Independent contract tests for #780 without PostgreSQL: the input
versions this build reads and the bytes a Run without standing instructions
sends (B4-10 / 780-4 / 780-5).

Written from the contract only (pre-review A section #780), without reading
the implementation. Pinned values were computed on origin/main ``d0a430bc``
(before any #780 code) with the helpers of
``tests/test_tool_expansion_c2b_contract.py``: a switch-on extension input
with a three-entry runbook catalog (input v7) and the switch-on revision.
No network, no model.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from opspilot.investigation.context import (
    KNOWN_INPUT_VERSIONS,
    InvestigationInput,
    initial_messages,
)
from opspilot.investigation.loop import prompt_revision_versions
from opspilot.tools.registry import canonical
from tests import test_tool_expansion_c2b_contract as c2b

V9 = "opspilot-investigation-input-v9"
EXT_VARIANT = "replay-candidate-k8s"
#: ``prompt_revision_versions(variant, knowledge_tool=True)`` on main.
SWITCH_ON_REVISIONS = {
    "replay-candidate-k8s": "prompt-replay-candidate-k8s-566b5c603da3",
    "replay-candidate": "prompt-replay-candidate-8e2d9bb575b5",
}
#: sha256 of ``canonical(initial_messages(...))`` and of the input's
#: ``canonical(as_json())`` for the v7 catalog input below, on main.
V7_MESSAGES_SHA256 = "281dd6e04b12bab38982eb04e324ecad57065558016d50f5f0346b3842ad5bc2"
V7_INPUT_SHA256 = "56dc5e0dfeddb6f551ec386faf29f2a35ba95b473402368b52a640b36e4de030"
CONTEXT = {"time_policies": [], "marker": "evidence-context"}


@pytest.fixture(autouse=True)
def _environment(tmp_path, monkeypatch):
    for name in (c2b.SWITCH, c2b.LIVE_SWITCH, *c2b.BACKEND_URLS):
        monkeypatch.delenv(name, raising=False)
    placeholder = tmp_path / "synthetic-ca-placeholder.pem"
    placeholder.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C2B_TEST_PLACEHOLDER_CA", str(placeholder))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _v7_input() -> InvestigationInput:
    base = c2b._input(c2b._env(switch="1"))
    return c2b._with_facts(base, **{c2b.CATALOG_KEY: c2b._catalog(c2b.A, c2b.B, c2b.C)})


def test_this_build_reads_v9_and_every_earlier_input_version():
    for n in range(1, 10):
        assert f"opspilot-investigation-input-v{n}" in KNOWN_INPUT_VERSIONS, n
    assert len(set(KNOWN_INPUT_VERSIONS)) == len(KNOWN_INPUT_VERSIONS)


def test_a_v7_catalog_input_without_instructions_is_byte_identical():
    built = _v7_input()
    assert built.version == "opspilot-investigation-input-v7"
    assert _sha(canonical(built.as_json())) == V7_INPUT_SHA256
    messages, _ = initial_messages(built, evidence_context=CONTEXT)
    assert _sha(canonical(messages)) == V7_MESSAGES_SHA256
    # A recorded v7 row still reads and rebuilds the same bytes.
    again = InvestigationInput.from_json(json.loads(json.dumps(built.as_json())))
    rebuilt, _ = initial_messages(again, evidence_context=CONTEXT)
    assert _sha(canonical(rebuilt)) == V7_MESSAGES_SHA256


def test_a_v1_input_still_reads():
    built = c2b._input(c2b._env())
    assert built.version == "opspilot-investigation-input-v1"
    again = InvestigationInput.from_json(json.loads(json.dumps(built.as_json())))
    assert canonical(again.as_json()) == canonical(built.as_json())


@pytest.mark.parametrize("variant", sorted(SWITCH_ON_REVISIONS))
def test_the_switch_on_revision_without_instructions_is_unchanged(variant):
    # 780-4: the new boundary is counted only for a Run whose input carries
    # the fact, behind a new keyword that defaults off -- every existing
    # caller keeps today's value.
    assert prompt_revision_versions(variant, knowledge_tool=True) == {
        "prompt_revision": SWITCH_ON_REVISIONS[variant]
    }
    assert prompt_revision_versions(variant) == {
        "prompt_revision": c2b.EXT_PROMPT_REVISION
        if variant == EXT_VARIANT
        else c2b.BASE_PROMPT_REVISION
    }


def test_the_profile_versions_are_unchanged_with_the_switch_on():
    # The web records ``profile.versions()`` for every Run; with no
    # instructions those must stay what they were (B4-10).
    from opspilot.tools.profiles import select_profile

    on = select_profile(c2b._env(switch="1")).versions()
    assert on["prompt_revision"] == SWITCH_ON_REVISIONS[EXT_VARIANT]
    off = select_profile(c2b._env()).versions()
    assert off["prompt_revision"] == c2b.EXT_PROMPT_REVISION
