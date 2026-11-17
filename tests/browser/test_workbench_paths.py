"""Workbench redesign browser paths (contract §5: at most three, this file).

Real PostgreSQL, the product web app served over HTTP on a free local port,
a real Chromium driven the way a user would: sign in, open the incident,
expand things by their visible names, read what is shown. Assertions are on
what the user can see, never on markup, CSS classes or element counts.

Path (a) (P4): open the detail page -> expand one log step and one query ->
see the raw result the tool returned. Paths (b) and (c) are added by P4 (with
P2) and P7.
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import threading
import time
from uuid import UUID

import pytest

from opspilot.tools import fixture
from opspilot.web import AuthConfig, Authenticator, create_app, hash_password
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


# contract P4 path (a) locator
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


def test_path_a_detail_step_query_raw_result(incident, served, browser_page):
    incident_id = incident
    page = browser_page

    # Sign in through the login form and land on the incident page.
    response = page.goto(f"{served}/incidents/{incident_id}")
    assert response is not None and response.ok
    page.get_by_label("User name").fill(UI_USER)
    page.get_by_label("Password").fill(UI_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(re.compile(rf"/incidents/{incident_id}$"))

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
