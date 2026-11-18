"""Workbench redesign browser paths (contract §5: at most three, this file).

Real PostgreSQL, the product web app served over HTTP on a free local port,
a real Chromium driven the way a user would: sign in, open the incident,
expand things by their visible names, read what is shown. Assertions are on
what the user can see, never on markup, CSS classes or element counts.

Path (a) (P4): open the detail page -> expand one log step and one query ->
see the raw result the tool returned.
Path (b) (P4, with P2): send a message to the investigation -> it is shown as
queued -> the agent's reply appears in the Investigation log.
Path (c) (P7): ask the Chat panel -> the answer appears there and no Run
starts; a steering sentence -> the panel says it was sent to the
investigation and the investigation's Queued shows the user's sentence.
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from uuid import UUID

import pytest

from opspilot.tools import fixture
from opspilot.web import AuthConfig, Authenticator, create_app, hash_password
from tests.m1_investigation_support import reply
from tests.workbench_driver import (  # noqa: F401 -- shared pytest fixtures
    UI_PASSWORD,
    UI_USER,
    workbench_database_fixture,
    workbench_fixture,
)

pytest.importorskip("playwright.sync_api", reason="Python Playwright is not installed")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

# A label value that is HTML if a page ever renders raw text unescaped: the
# user must see these exact characters (also covers the evidence-escaping
# case retired with tests/integration/test_f12_followup_impl_postgres.py).
RAW_MARKER = "<b>workbench-raw-marker</b>"
RAW_VALUE = "0.0427"
RAW_BODY = json.dumps(
    {"data": {"result": [{"metric": {"label": RAW_MARKER}, "value": float(RAW_VALUE)}]}}
).encode()


# contract P4 path (a) locator (also path (b))
# The contract fixes the flow, not the accessible names. These are the only
# names the test depends on; wire the page to them (or change them here),
# never the assertions below.
class Detail:
    def __init__(self, page):
        self.page = page

    def log(self):
        # The investigation log, a landmark region named by its heading.
        return self.page.get_by_role(
            "region", name=re.compile(r"investigation log", re.I)
        )

    @staticmethod
    def _toggle(scope, name):
        # A disclosure control: a button with that name, or the visible
        # summary text of a native <details> (no ARIA role in Playwright).
        return scope.get_by_role("button", name=name).or_(scope.get_by_text(name)).first

    def step(self):
        # The log step that ran the scripted tool, named by its tool.
        return self._toggle(self.log(), re.compile(r"metrics[._]range_query"))

    def query(self):
        # The query inside that step, named by its query expression.
        return self._toggle(self.log(), re.compile(re.escape(fixture.FIXTURE_EXPR)))

    # path (b)
    def composer(self):
        # The investigation input below the log (not the P7 Chat panel's box):
        # a text box whose accessible name says it messages the investigation.
        return self.page.get_by_role(
            "textbox", name=re.compile(r"message.*investigation", re.I)
        )

    def send(self):
        # The composer's submit control, as the user sees it.
        return (
            self.composer()
            .locator("xpath=ancestor::form[1]")
            .get_by_role("button", name=re.compile(r"^\s*send\b", re.I))
        )

    def queued(self):
        # The "Queued requests" area: a landmark region named by its heading.
        return self.page.get_by_role("region", name=re.compile(r"queued", re.I))

    # contract P7 path (c) locator
    def chat(self):
        # The right-side Chat panel: a complementary landmark named "Chat".
        return self.page.get_by_role(
            "complementary", name=re.compile(r"^\s*chat\b", re.I)
        )

    def ask(self):
        # The Chat panel's question box, named like "Ask".
        return self.chat().get_by_role("textbox", name=re.compile(r"\bask\b", re.I))

    def ask_send(self):
        # The Chat panel's submit control.
        return self.chat().get_by_role("button", name=re.compile(r"^\s*send\b", re.I))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(name="served")
def served_fixture(workbench):
    """The product app over real HTTP, with the login form enabled."""
    import uvicorn

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    auth = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={},
        auth_revision="workbench-browser",
        allowed_origins=frozenset({base}),
        session_secret=secrets.token_bytes(32),
    )
    app = create_app(workbench.workbench, Authenticator(auth), workbench.clock)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, name="workbench-browser", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            pytest.fail("workbench app did not start")
        time.sleep(0.05)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(name="incident")
def incident_fixture(workbench, monkeypatch):
    """A published incident with one committed tool step and its evidence.

    It enters through intake and the product runner with the scripted
    model; the fixture tool returns RAW_BODY. Created before the browser
    starts: Playwright's sync API owns an event loop the driver's in-process
    HTTP call cannot share.
    """
    monkeypatch.setattr(fixture, "_CANNED_BODY", RAW_BODY)
    scenario = workbench.published("workbench-browser-path-a")
    return UUID(scenario.subject_id)


@pytest.fixture(name="browser_page")
def browser_page_fixture(incident):
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except PlaywrightError as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip(
                    "Chromium for Playwright is not installed "
                    "(python -m playwright install --only-shell chromium)"
                )
            raise
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(10_000)
            yield page
        finally:
            browser.close()


def _sign_in(page, served, incident_id):
    """Sign in through the login form and land on the incident page."""
    response = page.goto(f"{served}/incidents/{incident_id}")
    assert response is not None and response.ok
    page.get_by_label("User name").fill(UI_USER)
    page.get_by_label("Password").fill(UI_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(re.compile(rf"/incidents/{incident_id}$"))


def test_path_a_detail_step_query_raw_result(incident, served, browser_page):
    incident_id = incident
    page = browser_page
    _sign_in(page, served, incident_id)

    detail = Detail(page)
    log = detail.log()
    expect(log).to_be_visible()

    # The raw result is not on screen until the user opens the step and query.
    expect(log.get_by_text(RAW_MARKER)).not_to_be_visible()

    detail.step().click()
    detail.query().click()

    # The rows the tool returned, as the tool returned them (escaped text).
    expect(log.get_by_text(RAW_MARKER)).to_be_visible()
    expect(log.get_by_text(RAW_VALUE)).to_be_visible()

    # Red line: next steps are advisory; nothing on the page applies or runs them.
    command = re.compile(r"^\s*(apply|run)\b", re.I)
    expect(page.get_by_role("button", name=command)).to_have_count(0)
    expect(page.get_by_role("link", name=command)).to_have_count(0)


MESSAGE = "workbench-path-b: which deploy changed the checkout error rate?"
REPLY = "workbench-path-b: the scripted agent answers the queued message."
DRAFT = "workbench-path-b: a draft the live update must keep."


def test_path_b_message_queued_then_reply_in_log(
    workbench, incident, served, browser_page
):
    """D3/B7/C6: an idle incident with a published conclusion takes a message
    from the investigation composer; it is queued until a Run reads it, and
    the reply then shows in the Investigation log without expanding a step
    (U3). The published conclusion stays on the page."""
    incident_id = incident
    page = browser_page
    published = workbench.terminal(incident_id)["report_content"]
    conclusion = json.loads(published)["summary"]
    _sign_in(page, served, incident_id)
    detail = Detail(page)
    expect(page.get_by_text(conclusion).first).to_be_visible()

    # The user writes to the investigation and sends it.
    composer = detail.composer()
    expect(composer).to_be_enabled()
    composer.fill(MESSAGE)
    detail.send().click()

    # Back on the page, the message waits under Queued.
    page.wait_for_url(re.compile(rf"/incidents/{incident_id}(\?.*)?(#.*)?$"))
    expect(detail.queued().get_by_text(MESSAGE)).to_be_visible()
    expect(page.get_by_text(conclusion).first).to_be_visible()

    # The product picks it up: the request already opened the continuation
    # Run (R2); the product runner runs it with a scripted model that answers
    # the message's input sequence in the m0-response-v1 envelope (C6).
    (message,) = workbench.unread(incident_id)
    assert message["content"]["text"] == MESSAGE
    answer = reply(
        content=json.dumps(
            {
                "schema_version": "m0-response-v1",
                "replies": [
                    {
                        "to_input_seq": message["sequence"],
                        "text": REPLY,
                        "evidence_ids": [],
                    }
                ],
                "report": None,
            }
        )
    )
    # P3-1/6/14: while the Run works the reader keeps the page open: the
    # earlier Run's step is still in the one log, opened to its raw result,
    # and a draft is in the composer. Nothing reloads the page under them.
    detail.step().click()
    detail.query().click()
    expect(detail.log().get_by_text(RAW_MARKER)).to_be_visible()
    composer = detail.composer()
    expect(composer).to_be_enabled()
    composer.fill(DRAFT)

    # The events of this Run reach the served app's progress stream: the
    # runner and the served app share one PostgreSQL event log.
    workbench.run(SimpleNamespace(subject_id=str(incident_id)), [answer])

    log = detail.log()
    expect(log).to_be_visible()
    # The reply arrives live (no page.reload()), readable in the log as is,
    # next to the message it answers.
    expect(log.get_by_text(REPLY)).to_be_visible()
    # P3-14: the live update kept what the reader had open and typed.
    expect(detail.composer()).to_have_value(DRAFT)
    expect(log.get_by_text(RAW_MARKER)).to_be_visible()
    expect(log.get_by_text(MESSAGE)).to_be_visible()
    # Read by a Run: no longer queued.
    expect(detail.queued().get_by_text(MESSAGE)).not_to_be_visible()
    # A reply-only Run leaves the published conclusion in place.
    expect(page.get_by_text(conclusion).first).to_be_visible()


QUESTION = "workbench-path-c: what does the report say caused the errors?"
ANSWER = "workbench-path-c: the scripted chat answers from the record."
STEER = "workbench-path-c: please check the payment-db connection pool next."
STEER_ANSWER = "workbench-path-c: I will pass that to the investigation."


def _chat_reply(text, *, steer):
    return reply(content=json.dumps({"answer": text, "steer": steer, "refs": []}))


def _wait_for(condition: Callable[[], bool], what: str, seconds: float = 10.0):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        time.sleep(0.1)


def test_path_c_chat_answer_then_steer_forwarded(
    workbench, incident, served, browser_page
):
    """E3–E5, P7-9/10/13: the Chat panel answers from the record without
    starting a Run; a steering sentence is forwarded as the user's own
    words and waits in the investigation's Queued."""
    incident_id = incident
    page = browser_page
    _sign_in(page, served, incident_id)
    detail = Detail(page)
    runs_before = len(workbench.runs(incident_id))

    # A plain question in the Chat panel.
    expect(detail.chat()).to_be_visible()
    expect(detail.ask()).to_be_enabled()
    detail.ask().fill(QUESTION)
    detail.ask_send().click()
    _wait_for(
        lambda: any(
            t["question"] == QUESTION for t in workbench.chat_turns(incident_id)
        ),
        "the question to be stored",
    )
    # The worker answers it (scripted model, no provider).
    workbench.chat_answer([_chat_reply(ANSWER, steer=False)])
    expect(detail.chat().get_by_text(ANSWER)).to_be_visible()
    assert len(workbench.runs(incident_id)) == runs_before, (
        "a plain Chat question must not start a Run"
    )
    expect(detail.queued().get_by_text(QUESTION)).not_to_be_visible()

    # A steering sentence: the panel forwards the user's own words.
    detail.ask().fill(STEER)
    detail.ask_send().click()
    _wait_for(
        lambda: any(t["question"] == STEER for t in workbench.chat_turns(incident_id)),
        "the steering sentence to be stored",
    )
    workbench.chat_answer([_chat_reply(STEER_ANSWER, steer=True)])
    expect(detail.chat().get_by_text(STEER_ANSWER)).to_be_visible()
    expect(
        detail.chat()
        .get_by_text(re.compile(r"sent to (the )?investigation", re.I))
        .first
    ).to_be_visible()
    expect(detail.queued().get_by_text(STEER)).to_be_visible()
    # The model's words are not what was queued.
    expect(detail.queued().get_by_text(STEER_ANSWER)).not_to_be_visible()
