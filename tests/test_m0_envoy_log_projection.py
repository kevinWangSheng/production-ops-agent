from scripts.m0_environment.holmes_baseline import _envoy_access_fields, log_projection

BODY = '[2026-09-11T12:08:29.187Z] "GET /api/data HTTP/1.1" 500 - via_upstream - "-" 0 129 90 89 "-" "ua" "trace" "frontend-proxy:8080" "upstream" frontend host - - -\n'


def test_envoy_positional_fields_are_labeled():
    value = _envoy_access_fields(BODY)
    assert value["response_status"] == 500
    assert value["bytes_received"] == 0
    assert value["bytes_sent"] == 129
    assert value["duration_ms"] == 90
    assert value["upstream_service_time_ms"] == 89


def test_envoy_parser_rejects_embedded_or_truncated_text():
    assert _envoy_access_fields("prefix " + BODY.strip()) is None
    assert _envoy_access_fields(BODY.replace(" 90 89 ", " 90 ")) is None


def test_log_projection_labels_only_proxy_access_records():
    record = {
        "data": {
            "data": {
                "hits": {
                    "total": {"value": 1},
                    "hits": [
                        {
                            "_id": "x",
                            "_source": {
                                "attributes": {"event.name": "proxy.access"},
                                "resource": {
                                    "service.name": "frontend-proxy",
                                    "log_name": "otel_envoy_access_log",
                                },
                                "body": BODY,
                            },
                        }
                    ],
                },
            },
        },
    }
    view = log_projection(record)
    row = view["data"]["displayed_logs"][0]
    assert row["envoy_access_fields"]["duration_ms"] == 90


def test_v3_replay_preserves_visible_rows_and_v4_labels():
    import json
    from pathlib import Path

    raw = json.loads(
        Path("tests/fixtures/m0_environment/normal02_log_raw.json").read_text()
    )
    v3 = log_projection(raw, version="m0-02-logs-v3")
    assert v3["data"]["model_visible_hit_count"] == 19
    v4 = log_projection(raw)
    assert v4["data"]["model_visible_hit_count"] == len(v4["data"]["displayed_logs"])
    assert (
        v4["data"]["model_visible_hit_count"] + v4["data"]["omitted_returned_hit_count"]
        == v4["data"]["backend_returned_hit_count"]
    )
