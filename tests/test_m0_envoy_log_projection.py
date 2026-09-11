from scripts.m0_environment.holmes_baseline import _envoy_access_fields


def test_envoy_positional_fields_are_labeled():
    body = '[2026-09-11T12:08:29Z] "GET /api/data HTTP/1.1" 200 - via_upstream - "-" 0 129 90 89 "-" "ua" "trace" "frontend-proxy:8080" "upstream" frontend host - - -\n'
    value = _envoy_access_fields(body)
    assert value["response_status"] == 200
    assert value["bytes_received"] == 0
    assert value["bytes_sent"] == 129
    assert value["duration_ms"] == 90
    assert value["upstream_service_time_ms"] == 89
