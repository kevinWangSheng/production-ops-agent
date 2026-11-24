"""Resume's hint says what it does in each state: under human ownership it
lifts the pause only and never hands the incident back (C3 §10, #124)."""

from opspilot.web.incident_view import _actions
from opspilot.web.status import _STATUSES


def _hint(status: str) -> str:
    view = _actions(_STATUSES[status], ["resume"], {})
    (item,) = [i for i in view["primary"] + view["more"] if i["action"] == "resume"]
    return item["hint"]


def test_resume_hint_matches_state() -> None:
    assert _hint("human_owned") == "Lifts the pause only; the Agent stays stopped."
    assert "queue" in _hint("paused")
