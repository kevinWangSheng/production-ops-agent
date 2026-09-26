"""A transport refusal that never left the process is audited as such.

The transport keeps its ``source_status`` contract for pre-dispatch refusals
(the registration classifies the fixed code); ``TransportResponse.sent`` is
what tells the executor that no request was made, so the audit record says
``sent: false`` and ``source_contact: none`` instead of claiming a confirmed
contact the source never had (independent review finding on the OTel profile).
"""

from opspilot.tools import TransportResponse
from tests.m1_tool_support import FakeTransport, build, registration, request


def _refusing(sent):
    return FakeTransport(
        response=TransportResponse(
            body=b'{"error":"QUERY_OUT_OF_WINDOW"}',
            source_status="QUERY_OUT_OF_WINDOW",
            sent=sent,
        )
    )


def _registration():
    return registration(error_classes={"QUERY_OUT_OF_WINDOW": "INVALID_PARAMS"})


def test_a_refusal_decided_before_sending_is_not_a_source_contact():
    executor, transport, sink, ledger = build(
        registrations=[_registration()], transport=_refusing(sent=False)
    )
    outcome = executor.execute(request())
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert outcome.source_contact == "none"
    assert outcome.operation.sent is False
    assert outcome.evidence is None and sink.records == []
    assert outcome.model_view["source_contact"] == "none"
    # The proposal was still a real gateway decision: it stays counted.
    assert executor.operations_used == 1


def test_a_status_the_source_really_returned_stays_a_confirmed_contact():
    executor, transport, sink, ledger = build(
        registrations=[_registration()], transport=_refusing(sent=True)
    )
    outcome = executor.execute(request())
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert outcome.source_contact == "confirmed"
    assert outcome.operation.sent is True


def test_sent_false_without_a_status_is_not_a_refusal():
    # A well-formed body with sent=False is contradictory transport data;
    # it is inspected like any other response, not treated as a refusal.
    transport = FakeTransport(
        response=TransportResponse(
            body=b'{"data": {"result": [{"metric": {}, "value": 1}]}}', sent=False
        )
    )
    executor, _, _, _ = build(registrations=[_registration()], transport=transport)
    outcome = executor.execute(request())
    assert outcome.status == "ok" and outcome.source_contact == "confirmed"
