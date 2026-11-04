"""The frozen soak schedule (S-R6).

Background: the OTel Demo loadgenerator stays on. Target-side faults: one
``paymentFailure`` episode every 3 h, 24 in all, each on for 15 min; episodes
8 and 16 stay on for 75 min and take the "observation ended unconfirmed"
branch (the profile observes for at most 3600 s). Four noise toggles
(``adManualGc`` and ``loadGeneratorFloodHomepage`` twice each, U2: they must
not open a Run) sit 2 h after an episode starts, after its observation is
over. Agent-side faults A1–A5 once each, all inside the middle 48 h
(12 h – 60 h) and at least 2 h apart: A1 kills the worker while a Run is in
flight, A2 restarts PostgreSQL while idle, A3 cuts the model egress for
20 min over an episode, A4 stops the tunnel for 10 min over an episode's
alert, A5 restarts the web.

T0 is chosen and frozen into the plan before the soak starts; every step
time is an offset from it and the runner waits for it. ``build_plan`` is
deterministic; ``freeze`` writes the canonical bytes once and returns their
SHA-256, which goes into the task record before T0 and never changes after.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.soak.common import DURATION_S, HOUR, iso

SCHEMA = "opspilot-soak-plan/2"
PRODUCT_ALERTNAME = "CheckoutPlaceOrderErrorRatioHigh"
MINUTE = 60
FIRST_EPISODE_S = 10 * MINUTE
EPISODE_INTERVAL_S = 3 * HOUR
EPISODES = 24
EPISODE_ON_S = 15 * MINUTE
LONG_EPISODES = frozenset({8, 16})
LONG_EPISODE_ON_S = 75 * MINUTE
EPISODE_FLAG = "paymentFailure"
#: (episode number, flag): the noise toggles, 2 h after that episode starts.
NOISE = (
    (3, "adManualGc"),
    (7, "loadGeneratorFloodHomepage"),
    (13, "adManualGc"),
    (19, "loadGeneratorFloodHomepage"),
)
NOISE_DELAY_S = 2 * HOUR
NOISE_ON_S = 15 * MINUTE
#: S-R6: the flags this soak moves; ``lab_fault.SOAK_FLAGS`` must equal it.
FLAGS = frozenset({EPISODE_FLAG, *(flag for _, flag in NOISE)})
MIDDLE_START_S = 12 * HOUR
MIDDLE_END_S = 60 * HOUR
AGENT_FAULT_SPACING_S = 2 * HOUR
#: How long a conditional step waits for its condition before running anyway
#: (the event log then says the condition was not met).
CONDITION_WAIT_S = 30 * MINUTE
#: A1: the worker is brought back this long after the kill when the restart
#: policy has not already done it.
WORKER_RECOVER_DELAY_S = 30


def _episode_at(number: int) -> int:
    return FIRST_EPISODE_S + (number - 1) * EPISODE_INTERVAL_S


def _step(
    step_id: str,
    action: str,
    *,
    at_s: int | None = None,
    after: str | None = None,
    delay_s: int = 0,
    condition: str | None = None,
    **params: Any,
) -> dict[str, Any]:
    step: dict[str, Any] = {"id": step_id, "action": action, **params}
    if at_s is not None:
        step["at_s"] = at_s
    else:
        step["after"] = after
        step["delay_s"] = delay_s
    if condition is not None:
        step["condition"] = condition
        step["max_wait_s"] = CONDITION_WAIT_S
    return step


def schedule() -> list[dict[str, Any]]:
    """The items with offsets from T0 (the part that does not depend on T0)."""
    items: list[dict[str, Any]] = []
    for number in range(1, EPISODES + 1):
        item_id = f"ep{number:02d}"
        on_s = LONG_EPISODE_ON_S if number in LONG_EPISODES else EPISODE_ON_S
        items.append(
            {
                "id": item_id,
                "kind": "episode",
                "flag": EPISODE_FLAG,
                "at_s": _episode_at(number),
                "duration_s": on_s,
                "branch": "unconfirmed" if number in LONG_EPISODES else "recovery",
                "steps": [
                    _step(
                        f"{item_id}:on",
                        "flag_on",
                        at_s=_episode_at(number),
                        flag=EPISODE_FLAG,
                    ),
                    _step(
                        f"{item_id}:off",
                        "flag_off",
                        after=f"{item_id}:on",
                        delay_s=on_s,
                        flag=EPISODE_FLAG,
                    ),
                ],
            }
        )
    for index, (number, flag) in enumerate(NOISE, start=1):
        item_id = f"nz{index}"
        at_s = _episode_at(number) + NOISE_DELAY_S
        items.append(
            {
                "id": item_id,
                "kind": "noise",
                "flag": flag,
                "at_s": at_s,
                "duration_s": NOISE_ON_S,
                "steps": [
                    _step(f"{item_id}:on", "flag_on", at_s=at_s, flag=flag),
                    _step(
                        f"{item_id}:off",
                        "flag_off",
                        after=f"{item_id}:on",
                        delay_s=NOISE_ON_S,
                        flag=flag,
                    ),
                ],
            }
        )
    a1 = _episode_at(6)
    a2 = _episode_at(9) + 130 * MINUTE
    a3 = _episode_at(12)
    a4 = _episode_at(15) + 3 * MINUTE
    a5 = _episode_at(18) + 130 * MINUTE
    items += [
        {
            "id": "A1",
            "kind": "agent_fault",
            "fault": "worker_kill",
            "at_s": a1,
            "overlaps": "ep06",
            "steps": [
                _step("A1:kill", "worker_kill", at_s=a1, condition="run_running"),
                _step(
                    "A1:recover",
                    "worker_up",
                    after="A1:kill",
                    delay_s=WORKER_RECOVER_DELAY_S,
                ),
            ],
        },
        {
            "id": "A2",
            "kind": "agent_fault",
            "fault": "pg_restart",
            "at_s": a2,
            "steps": [_step("A2:restart", "pg_restart", at_s=a2, condition="idle")],
        },
        {
            "id": "A3",
            "kind": "agent_fault",
            "fault": "egress_cut",
            "at_s": a3,
            "duration_s": 20 * MINUTE,
            "overlaps": "ep12",
            "steps": [
                _step("A3:cut", "egress_cut", at_s=a3),
                _step(
                    "A3:restore", "egress_restore", after="A3:cut", delay_s=20 * MINUTE
                ),
            ],
        },
        {
            "id": "A4",
            "kind": "agent_fault",
            "fault": "tunnel_stop",
            "at_s": a4,
            "duration_s": 10 * MINUTE,
            "overlaps": "ep15",
            "steps": [
                _step("A4:stop", "tunnel_stop", at_s=a4),
                _step("A4:start", "tunnel_start", after="A4:stop", delay_s=10 * MINUTE),
            ],
        },
        {
            "id": "A5",
            "kind": "agent_fault",
            "fault": "web_restart",
            "at_s": a5,
            "steps": [_step("A5:restart", "web_restart", at_s=a5)],
        },
    ]
    items.sort(key=lambda item: (item["at_s"], item["id"]))
    return items


def build_plan(
    t0: datetime,
    *,
    price_input: float,
    price_output: float,
    price_source: str,
    expected_deployments: Sequence[str],
) -> dict[str, Any]:
    """The frozen plan in the raw-directory format the summarizer reads
    (``summarize`` module docstring): absolute planned starts from ``t0``,
    plus each injection's executable ``steps`` (offsets from ``t0``).

    ``price_input`` is the cache-miss input price (the conservative one:
    the summarizer has a single input price)."""
    if t0.tzinfo is None:
        raise ValueError("t0 needs a time zone")
    injections = []
    for item in schedule():
        injections.append(
            {
                "id": item["id"],
                "category": item["kind"],
                "name": item.get("flag") or item.get("fault"),
                "planned_start": iso(t0 + timedelta(seconds=item["at_s"])),
                "planned_duration_s": int(item.get("duration_s", 0)),
                "at_s": item["at_s"],
                **{k: item[k] for k in ("branch", "overlaps") if k in item},
                "steps": item["steps"],
            }
        )
    return {
        "schema": SCHEMA,
        "t0": iso(t0),
        "duration_s": DURATION_S,
        "product_alertname": PRODUCT_ALERTNAME,
        "expected_deployments": sorted(expected_deployments),
        "model_price_cny_per_mtok": {"input": price_input, "output": price_output},
        "price_source": price_source,
        "injections": injections,
    }


def canonical(plan: dict[str, Any]) -> bytes:
    return (json.dumps(plan, sort_keys=True, indent=1) + "\n").encode()


def steps(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Every step of the plan with its item id, in item order."""
    return [
        {**step, "item": item["id"]}
        for item in plan["injections"]
        for step in item["steps"]
    ]


def freeze(path: Path, plan: dict[str, Any]) -> str:
    """Write the plan once; refuse to overwrite a different frozen plan."""
    data = canonical(plan)
    if path.exists():
        if path.read_bytes() != data:
            raise SystemExit(
                f"{path} holds a different plan; a frozen plan never changes"
            )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def load(path: Path) -> tuple[dict[str, Any], str]:
    data = path.read_bytes()
    plan = json.loads(data)
    if plan.get("schema") != SCHEMA:
        raise SystemExit(f"{path}: not a {SCHEMA} plan")
    return plan, hashlib.sha256(data).hexdigest()
