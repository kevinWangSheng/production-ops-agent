"""Offline engineer harness checks. No kubectl, DB, HTTP or model calls."""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from datetime import timedelta

import pytest

from opspilot.observation.release_policy import sha256
from opspilot.observer.prometheus import VectorResult
from opspilot.observer.release_evaluation import build_release_query_plan
from scripts import f11_u2_harness as h
from tests.f11_release_support import EvidenceBuilder, pods_for
from tests.test_f11_release_evaluation import AFTER_IMAGES, _context


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "LEDGERS", tmp_path / "ledgers")

    def forbidden(*args, **kwargs):
        pytest.fail("Offline test attempted external call")

    monkeypatch.setattr(h.subprocess, "run", forbidden)
    monkeypatch.setattr(h.psycopg, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def sample(
    context, end, *, after=None, context_values=None, missing=False, decision_delay=2
):
    pods = pods_for(AFTER_IMAGES)
    texts = build_release_query_plan(
        context, window_end=end, instance_uids=[p.uid for p in pods]
    )
    builder = EvidenceBuilder(window_end=end, texts=texts).sufficient(
        pods, after=after, context=context_values
    )
    evidence = builder.evidence()
    if missing:
        evidence = evidence.model_copy(update={"queries": ()})
    return h.CollectedSample(
        evidence=evidence,
        claimed_at=end,
        decision_at=end + timedelta(seconds=decision_delay),
        elapsed_seconds=float(decision_delay),
        query_timings=(),
    )


def series(context, count, *, start=None, spacing=62):
    start = start or context.authorized_at + timedelta(seconds=300)
    return [
        sample(context, start + timedelta(seconds=i * spacing)) for i in range(count)
    ]


def test_occupation_conflict_and_release(tmp_path):
    until = h.utc_now() + timedelta(hours=4)
    claimed = h.occupation("claim", "alice", "healthy-3", until)
    assert set(claimed) == {"holder", "batch", "started_at", "expected_end"}
    assert h.occupation("show", "", "") == claimed
    for action in ["claim", "check", "release"]:
        with pytest.raises(ValueError, match="LAB_OCCUPIED"):
            h.occupation(action, "bob", "eval", until)
    assert h.occupation("check", "alice", "healthy-3") == claimed
    h.occupation("release", "alice", "healthy-3")
    assert h.occupation("show", "", "") is None


def test_occupation_release_blocked_by_running_task_and_fault():
    h.occupation("claim", "alice", "batch", h.utc_now() + timedelta(hours=1))
    with h.reserved("alice", "batch"):
        with pytest.raises(ValueError, match="IN_PROGRESS"):
            h.occupation("release", "alice", "batch")
    h.write_json(h.LEDGERS / "fault-active.json", {})
    with pytest.raises(ValueError, match="IN_PROGRESS"):
        h.occupation("release", "alice", "batch")


def test_expired_record_cannot_be_stolen():
    h.write_json(
        h.LEDGERS / "occupation.json",
        dict(holder="alice", batch="batch", expected_end="2000-01-01T00:00:00Z"),
    )
    with pytest.raises(ValueError, match="LAB_OCCUPIED"):
        h.occupation("claim", "bob", "batch", h.utc_now() + timedelta(hours=1))


@pytest.mark.parametrize(
    "command",
    [
        ["release", "--run", "dry", "--release-id", "dry"],
        [
            "fault",
            "--run",
            "dry",
            "--offset",
            "300",
            "--duration",
            "60",
            "--flagd-url",
            "http://localhost:8013",
            "--method",
            "Int",
        ],
        ["watch", "--run", "dry"],
        ["collect", "--run", "dry"],
        [
            "register",
            "--run",
            "dry",
            "--registry",
            "absent.json",
            "--resource-uid",
            "registry-checkout",
            "--profile",
            "absent-profile.json",
            "--minimum",
            "1200",
        ],
        [
            "restore",
            "--run",
            "dry",
            "--flagd-url",
            "http://localhost:8013",
            "--method",
            "Int",
        ],
        [
            "context",
            "--run",
            "dry",
            "--resource-uid",
            "registry-checkout",
            "--minimum",
            "1200",
        ],
        ["occupation", "claim", "--until", "2099-01-01T00:00:00Z"],
    ],
)
def test_cli_dry_run_never_executes_or_writes(command, capsys):
    assert h.main(["--dry-run", *command]) == 0
    assert capsys.readouterr().out
    assert not h.LEDGERS.exists()


def test_live_command_requires_reservation_before_any_call():
    assert (
        h.main(
            [
                "--holder",
                "other",
                "--batch",
                "other",
                "release",
                "--run",
                "test",
                "--release-id",
                "r",
            ]
        )
        == 2
    )


class FixtureSource:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = []

    def instant_vector(self, expr, *, at, timeout_seconds):
        self.calls.append((expr, at, timeout_seconds))
        original = next(q for q in self.evidence.queries if q.query == expr)
        return VectorResult(
            expr=expr,
            status=original.status,
            series=(),
            body=original.raw,
            http_status=original.http_status,
            detail=original.detail,
            body_complete=original.body_complete,
            sent_at=at,
        )


def test_collect_serial_mapping_context_before_uid_after_and_hash_roundtrip(tmp_path):
    context = _context()
    end = context.authorized_at + timedelta(seconds=300)
    fixture = sample(context, end)
    source = FixtureSource(fixture.evidence)
    times = iter([end, end, end + timedelta(seconds=2)])
    captured = h.collect_sample(context, source, tmp_path, clock=lambda: next(times))
    keys = [q.query_key for q in captured.evidence.queries]
    base = build_release_query_plan(context, window_end=end, instance_uids=())
    assert keys[: len(base)] == list(base)
    assert all(k.startswith("after:") for k in keys[len(base) :])
    assert len(keys) == len(source.calls) == 49
    assert {call[1] for call in source.calls} == {end}
    assert all(0 < call[2] <= 20 for call in source.calls)
    h.save_sample(captured, tmp_path, 1)
    assert h.load_samples(tmp_path) == [captured]
    first = captured.query_timings[0]
    assert sha256((tmp_path / first["raw_path"]).read_bytes()) == first["raw_sha256"]
    (tmp_path / first["raw_path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="RAW_SHA256_MISMATCH"):
        h.load_samples(tmp_path)


def test_collect_lease_margin_skips_network_but_keeps_full_plan(tmp_path):
    context = _context()
    end = context.authorized_at + timedelta(seconds=300)
    source = FixtureSource(sample(context, end).evidence)
    ticks = iter([0.0, 116.0])

    def monotonic():
        return next(ticks, 116.0)

    captured = h.collect_sample(
        context, source, tmp_path, clock=lambda: end, monotonic=monotonic
    )
    assert not source.calls
    assert all(
        q.detail == "LEASE_BUDGET" and q.sent_at is None
        for q in captured.evidence.queries
    )
    assert len(captured.evidence.queries) == 33
    assert h.fold_samples(context, [captured], [600])["600"]["used_sample_count"] == 1


def test_arbitrary_response_bytes_survive_capture_reload(tmp_path):
    context = _context()
    end = context.authorized_at + timedelta(seconds=300)
    original = sample(context, end)

    class Broken(FixtureSource):
        def instant_vector(self, expr, *, at, timeout_seconds):
            return VectorResult(
                expr=expr,
                status="failed",
                series=(),
                body=b"\xff\xfe",
                http_status=502,
                detail="HTTP",
                body_complete=False,
                sent_at=at,
            )

    captured = h.collect_sample(
        context, Broken(original.evidence), tmp_path, clock=lambda: end
    )
    h.save_sample(captured, tmp_path, 1)
    assert h.load_samples(tmp_path)[0].evidence.queries[0].raw == b"\xff\xfe"


def test_fold_candidates_use_real_commits_and_health_reserve():
    context = _context()
    samples = series(context, 25)
    result = h.fold_samples(context, samples, [600, 1200, 2400])
    assert result["600"]["status"] == "healthy"
    assert result["1200"]["status"] == "healthy"
    assert h.utc(result["1200"]["terminal_at"]) > h.utc(result["600"]["terminal_at"])
    assert result["1200"]["healthy_reserve_ok"] is True
    assert result["2400"]["reason"] == "CAPTURE_INCOMPLETE"


def test_after_degraded_is_anomalous_without_waiting_for_m():
    context = _context()
    samples = [
        sample(
            context,
            context.authorized_at + timedelta(seconds=300),
            after={"error_ratio": 0.5},
        )
    ]
    result = h.fold_samples(context, samples, [2400])["2400"]
    assert result["status"] == "anomalous"
    assert result["used_sample_count"] == 1
    assert result["terminal_at"] == samples[0].decision_at.isoformat()


def test_context_degraded_stays_unknown_and_unknown_resets_window():
    context = _context()
    samples = series(context, 8)
    end = samples[-1].evidence.window.end + timedelta(seconds=62)
    samples.append(
        sample(context, end, context_values={"deployment_ready_replicas": 0.0})
    )
    samples.extend(series(context, 3, start=end + timedelta(seconds=62)))
    result = h.fold_samples(context, samples, [600])["600"]
    assert result["status"] == "unknown"
    assert "CONTEXT_DEGRADED_ONLY" in result["unknown_reasons"]
    assert all(v is None for v in result["samples"][8]["marks"].values())


def test_gap_resets_tracking_and_healthy_windows():
    context = _context()
    samples = series(context, 8)
    samples.append(
        sample(context, samples[-1].evidence.window.end + timedelta(seconds=400))
    )
    result = h.fold_samples(context, samples, [600])["600"]
    assert result["status"] == "unknown"
    assert len(result["coverage_gaps"]) == 1
    assert (
        result["samples"][-1]["marks"]["healthy_since"]
        == samples[-1].evidence.window.end.isoformat()
    )


def test_empty_unknown_counts_partial_nonempty_does_not_and_late_lease_does_not():
    context = _context()
    one, two, three = series(context, 3)
    empty = replace(one, evidence=one.evidence.model_copy(update={"queries": ()}))
    partial = replace(
        two,
        evidence=two.evidence.model_copy(update={"queries": two.evidence.queries[:1]}),
    )
    late = replace(three, decision_at=three.claimed_at + timedelta(seconds=120))
    result = h.fold_samples(context, [empty, partial, late], [600])["600"]
    assert result["used_sample_count"] == 1
    assert [r["adoption_reason"] for r in result["samples"]] == [
        "adopted",
        "readings_inconsistent",
        "lease_revoked",
    ]


def test_budget_unknown_and_healthy_wins_on_last_sample():
    context = _context()
    context = context.model_copy(
        update={"release": context.release.model_copy(update={"max_samples": 11})}
    )
    healthy = h.fold_samples(context, series(context, 11), [600])["600"]
    assert healthy["status"] == "healthy" and healthy["remaining_samples"] == 0
    samples = [
        replace(s, evidence=s.evidence.model_copy(update={"queries": ()}))
        for s in series(context, 11)
    ]
    exhausted = h.fold_samples(context, samples, [600])["600"]
    assert (
        exhausted["status"] == "unknown"
        and exhausted["reason"] == "max_samples_exhausted"
    )


def test_deadline_equality_refuses_even_degraded_and_future_schedule_rejected():
    context = _context()
    sample_at_deadline = sample(
        context,
        context.release.deadline_at - timedelta(seconds=2),
        after={"error_ratio": 0.5},
    )
    assert (
        h.fold_samples(context, [sample_at_deadline], [600])["600"]["reason"]
        == "deadline_expired"
    )
    too_early = sample(context, context.authorized_at + timedelta(seconds=59))
    with pytest.raises(ValueError, match="SCHEDULE"):
        h.fold_samples(context, [too_early], [600])


def test_summary_and_aggregate_include_failed_runs_without_approval(tmp_path):
    context = _context()
    samples = series(context, 2)
    summary = h.run_summary(
        context,
        samples,
        scenario="healthy",
        truth={"status": "unconfirmed"},
        timeline=[],
        candidates=[600],
        code_revision="test",
    )
    required = {
        "target",
        "profile_revision",
        "profile_sha256",
        "truth",
        "timeline",
        "candidates",
        "capture",
        "raw_response_hashes",
    }
    assert required <= summary.keys()
    assert summary["candidates"]["600"]["status"] == "unknown"
    h.write_json(tmp_path / "round.json", summary)
    output = tmp_path / "summary.json"
    aggregate = h.aggregate_summaries([tmp_path / "round.json"], output)
    assert aggregate["approval"] is None
    assert aggregate["scenario_counts"] == {"healthy": 1}
    assert aggregate["runs"][0]["sha256"] == sha256(
        (tmp_path / "round.json").read_bytes()
    )
    with pytest.raises(FileExistsError):
        h.aggregate_summaries([tmp_path / "round.json"], output)


def identity_objects():
    target = dict(
        integration_id="test",
        cluster_uid="cluster",
        namespace="otel-demo",
        resource_uid="registry-key",
        workload="checkout",
    )
    registry = {
        "registry-key": {k: v for k, v in target.items() if k != "resource_uid"}
    }
    dep = dict(
        metadata=dict(
            name="checkout", namespace="otel-demo", uid="DIFFERENT_DEPLOYMENT_UID"
        ),
        spec=dict(
            template=dict(
                spec=dict(
                    containers=[
                        dict(
                            name="checkout",
                            env=[
                                dict(
                                    name="OTEL_RESOURCE_ATTRIBUTES",
                                    value="opspilot.integration.id=test",
                                )
                            ],
                        )
                    ]
                )
            )
        ),
    )
    return (
        target,
        registry,
        dict(metadata=dict(name="kube-system", uid="cluster")),
        dict(metadata=dict(name="otel-demo")),
        dep,
    )


def test_identity_uses_registry_key_never_deployment_uid():
    target, registry, cluster, ns, dep = identity_objects()
    assert (
        h.validate_identity(registry, "registry-key", target, cluster, ns, dep)
        == target
    )
    for key in h.TARGET_KEYS:
        bad = {**target, key: "wrong"}
        with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
            h.validate_identity(registry, "registry-key", bad, cluster, ns, dep)
    dep["spec"]["template"]["spec"]["containers"][0]["env"] = []
    with pytest.raises(ValueError, match="INTEGRATION"):
        h.validate_identity(registry, "registry-key", target, cluster, ns, dep)


def test_flag_mutation_refuses_unknown_ad_name_or_wrong_state():
    raw = json.dumps(
        {
            "flags": {
                "paymentFailure": {
                    "defaultVariant": "off",
                    "variants": {"off": 0, "100%": 100},
                }
            }
        }
    ).encode()
    injected = h.change_flag(raw, "paymentFailure", "100%", "off")
    assert json.loads(injected)["flags"]["paymentFailure"]["defaultVariant"] == "100%"
    with pytest.raises(ValueError, match="UNCONFIRMED"):
        h.change_flag(raw, "guessed-ad-flag", "on", "off")
    with pytest.raises(SystemExit):
        h.change_flag(injected, "paymentFailure", "100%", "off")


def test_history_only_valid_lease_still_defers_next_claim_by_cadence():
    context = _context()
    partial = series(context, 1)[0]
    partial = replace(
        partial,
        evidence=partial.evidence.model_copy(
            update={"queries": partial.evidence.queries[:1]}
        ),
    )
    early = sample(context, partial.decision_at + timedelta(seconds=1))
    with pytest.raises(ValueError, match="INVALID_SAMPLE_SCHEDULE"):
        h.fold_samples(context, [partial, early], [600])


def test_true_deadline_capture_end_distinct_from_truncated_ledger():
    context = _context()
    samples = series(context, 2)
    incomplete = h.fold_samples(context, samples, [600])["600"]
    assert incomplete["terminal_at"] is None
    final = h.fold_samples(
        context,
        samples,
        [600],
        capture_end=dict(
            reason="deadline_expired", at=context.release.deadline_at.isoformat()
        ),
    )["600"]
    assert final["reason"] == "deadline_expired"
    assert final["terminal_at"] == context.release.deadline_at.isoformat()


def deployment_snapshot(*, after=False, ready=True):
    import copy

    at = "2026-10-14T10:00:00+00:00"
    template = dict(
        metadata=dict(labels={"app": "checkout"}),
        spec=dict(
            containers=[
                dict(
                    name="checkout",
                    env=[
                        dict(
                            name="OPSPILOT_U2_RELEASE_ID",
                            value="new" if after else "old",
                        )
                    ],
                )
            ]
        ),
    )
    hash_value = "newhash" if after else "oldhash"
    rsuid = "rs-new" if after else "rs-old"
    rs = dict(
        metadata=dict(
            uid=rsuid,
            name="checkout-" + hash_value,
            labels={"pod-template-hash": hash_value},
            annotations={"deployment.kubernetes.io/revision": "2" if after else "1"},
        ),
        spec=dict(template=copy.deepcopy(template)),
    )
    rs["spec"]["template"]["metadata"]["labels"]["pod-template-hash"] = hash_value
    pod = dict(
        metadata=dict(
            uid="pod-new" if after else "pod-old",
            name="pod",
            ownerReferences=[dict(uid=rsuid, controller=True)],
        ),
        spec=dict(containers=[dict(name="checkout")]),
        status=dict(
            conditions=[
                dict(
                    type="Ready",
                    status="True" if ready else "False",
                    lastTransitionTime=at,
                )
            ],
            containerStatuses=[
                dict(name="checkout", imageID="repo@sha256:" + "a" * 64)
            ],
        ),
    )
    return dict(
        requested_at=at,
        observed_at=at,
        deployment=dict(
            metadata=dict(
                uid="dep",
                name="checkout",
                namespace="otel-demo",
                resourceVersion="1",
                generation=2 if after else 1,
                annotations={
                    "deployment.kubernetes.io/revision": "2" if after else "1"
                },
            ),
            spec=dict(template=template, replicas=1),
            status=dict(observedGeneration=2),
        ),
        replicasets=[rs],
        pods=[pod],
        events=[],
    )


def test_release_r_is_patch_return_and_watch_stores_uid_digest_ready_and_gone(
    monkeypatch, tmp_path
):
    before, after = deployment_snapshot(), deployment_snapshot(after=True)
    monkeypatch.setattr(h, "snapshot", lambda **kwargs: before)
    calls = []

    def patch(command, **kwargs):
        calls.append(command)
        return json.dumps(after["deployment"]).encode()

    monkeypatch.setattr(h, "external", patch)
    start = h.utc("2026-10-14T10:00:00Z")
    clocks = iter([start, start + timedelta(seconds=2)])
    monkeypatch.setattr(h, "utc_now", lambda: next(clocks))
    released = h.publish(tmp_path, "new")
    assert released["released_at"] == (start + timedelta(seconds=2)).isoformat()
    assert released["request_before"] == start.isoformat()
    assert calls[0][calls[0].index("--type=strategic")] == "--type=strategic"
    assert released["before_revision"]["images"] == {"checkout": "sha256:" + "a" * 64}
    monkeypatch.setattr(h, "snapshot", lambda **kwargs: after)
    watched = h.watch_timeline(tmp_path, 10)
    assert watched["after_pod_template_hash"] == "newhash"
    assert watched["after_replicaset_uid"] == "rs-new"
    events = h.read_events(tmp_path, "timeline.jsonl")
    assert {e["kind"] for e in events} >= {
        "release_patch",
        "new_pod_ready",
        "old_pod_gone",
    }
    assert next(e for e in events if e["kind"] == "old_pod_gone")["event_at"] is None


def test_fault_always_restores_after_effect_probe_failure(monkeypatch, tmp_path):
    h.write_json(tmp_path / "release.json", dict(released_at="2026-10-14T10:00:00Z"))
    raw = json.dumps(
        {
            "flags": {
                "paymentFailure": {
                    "defaultVariant": "off",
                    "variants": {"off": 0, "100%": 100},
                }
            }
        }
    ).encode()
    monkeypatch.setattr(h, "flags", lambda: ({}, raw))
    monkeypatch.setattr(h, "flagd_resolution", lambda *a: {"variant": "off"})
    monkeypatch.setattr(h, "patch_flags", lambda *a: None)
    monkeypatch.setattr(
        h,
        "await_flag",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("PROPAGATION")),
    )
    restores = []
    monkeypatch.setattr(h, "restore_fault", lambda *a: restores.append(a))
    with pytest.raises(ValueError, match="PROPAGATION"):
        h.scheduled_fault(
            tmp_path,
            offset=0,
            duration=1,
            flag="paymentFailure",
            variant="100%",
            normal="off",
            url="http://localhost:8013",
            method="Int",
        )
    assert len(restores) == 1
    assert any(
        e["kind"] == "injection_failed" for e in h.read_events(tmp_path, "fault.jsonl")
    )


def test_restore_refuses_to_overwrite_external_flag_change(monkeypatch, tmp_path):
    h.write_json(
        h.LEDGERS / "fault-active.json",
        dict(
            ledger=str(tmp_path.resolve()),
            original="original",
            injected="injected",
            flag="paymentFailure",
            normal="off",
        ),
    )
    monkeypatch.setattr(h, "flags", lambda: ({}, b"other-session-change"))
    with pytest.raises(ValueError, match="REFUSE_RESTORE"):
        h.restore_fault(tmp_path, "http://localhost:8013", "Int", 1)
    assert (h.LEDGERS / "fault-active.json").exists()


def test_aggregate_rejects_same_release_after_truth_edits(tmp_path):
    context = _context()
    a = h.run_summary(
        context,
        [],
        scenario="healthy",
        truth={"status": "unconfirmed"},
        timeline=[],
        candidates=[600],
        code_revision="test",
    )
    b = {**a, "truth": {"status": "checked"}}
    h.write_json(tmp_path / "a.json", a)
    h.write_json(tmp_path / "b.json", b)
    with pytest.raises(ValueError, match="DUPLICATE_RELEASE_RUN"):
        h.aggregate_summaries(
            [tmp_path / "a.json", tmp_path / "b.json"], tmp_path / "summary.json"
        )


def test_artifact_manifest_includes_all_evidence_without_hash_cycle(tmp_path):
    h.write_json(tmp_path / "context.json", {})
    h.write_json(tmp_path / "watch" / "snapshot.json", {})
    h.write_json(tmp_path / "summary.json", {})
    manifest = h.artifact_manifest(tmp_path)
    assert {a["path"] for a in manifest} == {"context.json", "watch/snapshot.json"}
    assert all(
        a["sha256"] == sha256((tmp_path / a["path"]).read_bytes()) for a in manifest
    )


def test_effect_bounds_do_not_invent_negative_probe_after_patch(monkeypatch, tmp_path):
    before = h.utc("2026-10-14T10:00:00Z")
    monkeypatch.setattr(h, "flagd_resolution", lambda *a: {"variant": "100%"})
    h.await_flag(
        tmp_path,
        "http://localhost:8013",
        "paymentFailure",
        "Int",
        "100%",
        1,
        not_before=before,
    )
    event = h.read_events(tmp_path, "fault.jsonl")[-1]
    assert event["lower_bound"] == before.isoformat()
    assert event["application_onset_at"] is None


def test_context_reads_registry_database_and_cluster_without_guessing_a(
    monkeypatch, tmp_path
):
    from uuid import uuid4

    from tests.f11_release_support import PROFILE

    target, registry, cluster, ns, _ = identity_objects()
    registry["registry-key"]["health_profile_id"] = PROFILE.profile_id
    registry_path = tmp_path / "registry.json"
    h.write_json(registry_path, registry)
    before, after = deployment_snapshot(), deployment_snapshot(after=True)
    after["deployment"]["spec"]["template"]["spec"]["containers"][0]["env"].append(
        dict(name="OTEL_RESOURCE_ATTRIBUTES", value="opspilot.integration.id=test")
    )
    released = h.utc("2026-10-14T10:00:00Z")
    authorized = released + timedelta(seconds=40)
    h.write_json(
        tmp_path / "release.json",
        dict(
            release_id="new",
            released_at=released.isoformat(),
            before_revision=h.revision_from_snapshot(before, before["replicasets"][0]),
            after_revision=h.revision_from_snapshot(after, after["replicasets"][0]),
            after_template=after["deployment"]["spec"]["template"],
            deployment_uid="dep",
            deployment_generation=2,
            before_replicaset_uid="rs-old",
            after_replicaset_uid="rs-new",
            before_pod_template_hash="oldhash",
            after_pod_template_hash="newhash",
        ),
    )
    monkeypatch.setattr(h, "snapshot", lambda: after)
    monkeypatch.setattr(
        h, "kube", lambda *args: cluster if "kube-system" in args else ns
    )
    monkeypatch.setattr(
        h, "db_snapshot", lambda *args: ({**target, "target_id": uuid4()}, authorized)
    )
    monkeypatch.setattr(h, "utc_now", lambda: authorized)
    context = h.candidate_context(
        tmp_path,
        registry_path,
        "registry-key",
        h.ROOT / "opspilot/observer/profiles/otel-demo-checkout.json",
        1200,
        "NOT_PRINTED",
    )
    assert (
        context.authorized_at == authorized
        and context.authorized_at != context.release.released_at
    )
    assert context.release.target_snapshot["resource_uid"] == "registry-key"
    assert context.release.auxiliary.deployment_uid == "dep"
    assert "UNAPPROVED" in context.policy.content.approval_ref
    assert context.policy.content.health_profile_sha256 == sha256(
        h.canonical_content(PROFILE).encode()
    )


def test_baseline_failure_remains_in_fault_log_without_injecting(monkeypatch, tmp_path):
    h.write_json(tmp_path / "release.json", dict(released_at="2026-10-14T10:00:00Z"))
    raw = json.dumps(
        {
            "flags": {
                "paymentFailure": {
                    "defaultVariant": "off",
                    "variants": {"off": 0, "100%": 100},
                }
            }
        }
    ).encode()
    monkeypatch.setattr(h, "flags", lambda: ({}, raw))
    monkeypatch.setattr(h, "flagd_resolution", lambda *a: {"variant": "not-normal"})
    with pytest.raises(ValueError, match="BASELINE_NOT_NORMAL"):
        h.scheduled_fault(
            tmp_path,
            offset=0,
            duration=1,
            flag="paymentFailure",
            variant="100%",
            normal="off",
            url="http://localhost:8013",
            method="Int",
        )
    events = h.read_events(tmp_path, "fault.jsonl")
    assert [e["kind"] for e in events] == ["injection_planned", "injection_failed"]
    assert not (h.LEDGERS / "fault-active.json").exists()


@pytest.mark.parametrize("fence_fails", [False, True])
def test_timed_out_injection_cannot_commit_later_without_recovery_record(
    monkeypatch, tmp_path, fence_fails
):
    import copy

    raw = json.dumps(
        {
            "flags": {
                "paymentFailure": {
                    "defaultVariant": "off",
                    "variants": {"off": 0, "100%": 100},
                }
            }
        }
    )
    config = {
        "metadata": {"resourceVersion": "1", "annotations": {}},
        "data": {h.kind_lab.FLAG_FILE: raw},
    }
    pending = []

    def apply(operations):
        changed = copy.deepcopy(config)
        for op in operations:
            pieces = [
                p.replace("~1", "/").replace("~0", "~")
                for p in op["path"].split("/")[1:]
            ]
            parent = changed
            for piece in pieces[:-1]:
                parent = parent[piece]
            if op["op"] == "test":
                if parent[pieces[-1]] != op["value"]:
                    raise ValueError("CAS_CONFLICT")
            else:
                parent[pieces[-1]] = op["value"]
        if changed != config:
            changed["metadata"]["resourceVersion"] = str(
                int(config["metadata"]["resourceVersion"]) + 1
            )
            config.update(changed)
        return copy.deepcopy(config)

    def kubernetes(*args, **kwargs):
        if "get" in args:
            return copy.deepcopy(config)
        operations = json.loads(args[args.index("-p") + 1])
        if not pending:
            pending.append(operations)
            raise ValueError("EXTERNAL_CALL_TIMEOUT")  # sent, still pending on server
        if fence_fails:
            raise ValueError("EXTERNAL_CALL_TIMEOUT")
        return apply(operations)

    monkeypatch.setattr(h, "kube", kubernetes)
    monkeypatch.setattr(
        h,
        "flagd_resolution",
        lambda *args: {
            "variant": json.loads(config["data"][h.kind_lab.FLAG_FILE])["flags"][
                "paymentFailure"
            ]["defaultVariant"]
        },
    )
    h.write_json(tmp_path / "release.json", {"released_at": "2026-10-14T10:00:00Z"})
    with pytest.raises(ValueError, match="EXTERNAL_CALL_TIMEOUT"):
        h.scheduled_fault(
            tmp_path,
            offset=0,
            duration=1,
            flag="paymentFailure",
            variant="100%",
            normal="off",
            url="http://localhost:8013",
            method="Int",
        )
    try:
        apply(pending[0])  # original request finally reaches the API server
    except ValueError as exc:
        assert str(exc) == "CAS_CONFLICT"
    current = json.loads(config["data"][h.kind_lab.FLAG_FILE])["flags"][
        "paymentFailure"
    ]["defaultVariant"]
    if fence_fails:
        assert (h.LEDGERS / "fault-active.json").exists(), (
            "uncertain restore must retain recovery inputs"
        )
    else:
        assert current == "off", "late injection escaped restoration"
        assert not (h.LEDGERS / "fault-active.json").exists()


def test_pod_disappearance_bounds_include_deletion_during_prior_get_response(
    monkeypatch, tmp_path
):
    import copy

    before, after = deployment_snapshot(), deployment_snapshot(after=True)
    t0 = h.utc("2026-10-14T10:00:00Z")
    now = [t0]
    calls = []

    def kubernetes(*args, **kwargs):
        index = len(calls)
        calls.append(args)
        source = before if index < 2 else after
        if "deployment" in args:
            return copy.deepcopy(source["deployment"])
        rs = copy.deepcopy(source["replicasets"][0])
        rs.update(kind="ReplicaSet")
        rs["metadata"]["ownerReferences"] = [{"uid": "dep", "controller": True}]
        pod = {**copy.deepcopy(source["pods"][0]), "kind": "Pod"}
        now[0] = t0 + timedelta(seconds=2 if index < 2 else 4)
        return {"items": [rs, pod]}

    monkeypatch.setattr(h, "kube", kubernetes)
    monkeypatch.setattr(h, "utc_now", lambda: now[0])
    h.write_json(tmp_path / "before.json", h.snapshot())
    h.write_json(
        tmp_path / "release.json",
        dict(
            after_template=after["deployment"]["spec"]["template"],
            deployment_uid="dep",
            deployment_generation=2,
            before_pod_template_hash="oldhash",
        ),
    )
    h.watch_timeline(tmp_path, 10)
    gone = next(
        e
        for e in h.read_events(tmp_path, "timeline.jsonl")
        if e["kind"] == "old_pod_gone"
    )
    deletion = t0 + timedelta(seconds=1)  # after source read, before GET response ended
    assert h.utc(gone["lower_bound"]) <= deletion <= h.utc(gone["upper_bound"])
    saved = h.read_json(tmp_path / "before.json")
    assert h.utc(saved["objects_requested_at"]) == t0
    assert h.utc(saved["objects_observed_at"]) == t0 + timedelta(seconds=2)


def test_summary_binds_capture_code_before_source_and_head_change(
    monkeypatch, tmp_path, capsys
):
    import copy
    from pathlib import Path

    root = tmp_path / "repo"
    script = root / "scripts/f11_u2_harness.py"
    script.parent.mkdir(parents=True)
    script.write_bytes(b"# capture implementation\n")
    lab_script = root / "scripts/kind_lab.py"
    lab_script.write_bytes(b"# lab implementation\n")
    product = root / "opspilot/observer/release_evaluation.py"
    product.parent.mkdir(parents=True)
    product.write_bytes(b"# product evaluator\n")
    (root / "uv.lock").write_bytes(b"# locked dependencies\n")
    (root / "pyproject.toml").write_bytes(b"# project\n")
    monkeypatch.setattr(h, "ROOT", root)
    monkeypatch.setattr(h, "__file__", str(script))
    monkeypatch.setattr(h.kind_lab, "__file__", str(lab_script))
    revision = ["capture-head"]

    def git(command, **kwargs):
        if "rev-parse" in command:
            return revision[0].encode()
        if "status" in command:
            return b"?? scripts/f11_u2_harness.py\x00"
        if "diff" in command:
            return b"mock worktree diff\n"
        pytest.fail("unexpected external action")

    monkeypatch.setattr(h, "external", git)
    context = _context()
    ledger = h.ledger_path("provenance")
    h.write_json(ledger / "context.json", context.model_dump(mode="json"))
    # End immediately, so the real collect_run start path executes without HTTP/DB.
    monkeypatch.setattr(h, "db_clock", lambda _: context.release.deadline_at)
    h.collect_run(ledger, context, object(), "unused")
    started = h.read_json(ledger / "capture-start.json")
    captured_bytes = script.read_bytes()
    frozen_start = copy.deepcopy(started)
    script.write_bytes(b"# later folding implementation\n")
    product.write_bytes(b"# revised evaluator\n")
    revision[0] = "fold-head"
    truth = tmp_path / "truth.json"
    h.write_json(truth, {"status": "unconfirmed"})
    assert (
        h.main(
            [
                "fold",
                "--run",
                "provenance",
                "--minimum",
                "600",
                "--scenario",
                "probe",
                "--truth",
                str(truth),
            ]
        )
        == 0
    )
    capsys.readouterr()
    summary = h.read_json(ledger / "summary.json")
    assert summary["code_revision"] == "capture-head", (
        "fold HEAD relabelled historical capture"
    )
    assert summary["harness_sha256"] == sha256(captured_bytes)
    assert summary["capture_code"] == frozen_start["code"]
    assert summary["fold_code"]["revision"] == "fold-head"
    assert summary["fold_code"]["source_sha256"]["scripts/f11_u2_harness.py"] == sha256(
        script.read_bytes()
    )
    assert h.read_json(ledger / "capture-start.json") == frozen_start
    frozen_script = ledger / "code-snapshots/capture/scripts/f11_u2_harness.py"
    assert frozen_script.read_bytes() == captured_bytes
    assert (
        ledger / "code-snapshots/capture/worktree.diff"
    ).read_bytes() == b"mock worktree diff\n"
    assert (
        Path(summary["capture_code"]["snapshot_path"]).as_posix()
        == "code-snapshots/capture"
    )


def test_missing_capture_provenance_never_borrows_fold_revision():
    summary = h.run_summary(
        _context(),
        [],
        scenario="probe",
        truth={},
        timeline=[],
        candidates=[600],
        code_revision="fold-only",
    )
    assert summary["capture_code_status"] == "unavailable"
    assert summary["code_revision"] is None and summary["harness_sha256"] is None
    assert summary["fold_code"]["revision"] == "fold-only"


def test_failed_fold_can_retry_with_corrected_truth_and_retains_both_snapshots(
    monkeypatch, tmp_path, capsys
):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    for name in (
        "scripts/f11_u2_harness.py",
        "scripts/kind_lab.py",
        "pyproject.toml",
        "uv.lock",
    ):
        (root / name).write_bytes(b"# source\n")
    monkeypatch.setattr(h, "ROOT", root)
    monkeypatch.setattr(
        h, "external", lambda command, **kw: b"head" if "rev-parse" in command else b""
    )
    ledger = h.ledger_path("retry")
    h.write_json(ledger / "context.json", _context().model_dump(mode="json"))
    truth = tmp_path / "truth.json"
    args = [
        "fold",
        "--run",
        "retry",
        "--minimum",
        "600",
        "--scenario",
        "probe",
        "--truth",
        str(truth),
    ]
    assert h.main(args) == 2  # truth absent after code snapshot was frozen
    h.write_json(truth, {})
    assert h.main(args) == 0
    capsys.readouterr()
    summary = h.read_json(ledger / "summary.json")
    attempts = list((ledger / "code-snapshots/fold").iterdir())
    assert len(attempts) == 2 and all(p.is_dir() for p in attempts)
    assert (ledger / summary["fold_code"]["snapshot_path"]).is_dir()


@pytest.fixture
def registration_inputs(tmp_path):
    from tests.f11_release_support import PROFILE

    ledger = h.ledger_path("register")
    before, after = deployment_snapshot(), deployment_snapshot(after=True)
    release = dict(
        release_id="u2-configuration-001",
        released_at="2026-10-14T10:00:00Z",
        before_revision=h.revision_from_snapshot(before, before["replicasets"][0]),
        after_revision=h.revision_from_snapshot(after, after["replicasets"][0]),
        before_template=before["deployment"]["spec"]["template"],
        after_template=after["deployment"]["spec"]["template"],
        deployment_uid="dep",
        deployment_generation=2,
        before_replicaset_uid="rs-old",
        after_replicaset_uid="rs-new",
        before_pod_template_hash="oldhash",
        after_pod_template_hash="newhash",
    )
    h.write_json(ledger / "release.json", release)
    target, registry, *_ = identity_objects()
    registry["registry-key"]["health_profile_id"] = PROFILE.profile_id
    registry_path = tmp_path / "registry.json"
    h.write_json(registry_path, registry)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(h.canonical_content(PROFILE))
    return ledger, registry_path, profile_path, release, target


@pytest.fixture
def registration_stores(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from opspilot.observation.releases import ReleaseReceipt

    target_id, observation_id = uuid4(), uuid4()
    calls, closed = [], []
    record = SimpleNamespace(
        released_at=h.utc("2026-10-14T10:00:00Z"),
        deadline_at=h.utc("2026-10-14T11:00:00Z"),
    )
    response = SimpleNamespace(status="pending", reason=None, no_observation=False)
    outcomes = ["created", "replayed"]

    class Targets:
        def __init__(self, dsn, **kwargs):
            pass

        def register_target(self, resource_uid):
            calls.append(("register_target", resource_uid))
            return target_id

        def close(self):
            closed.append("targets")

    class Releases:
        def __init__(self, dsn, **kwargs):
            pass

        def record_release(self, **kwargs):
            calls.append(("record_release", kwargs))
            return ReleaseReceipt(
                outcome=outcomes.pop(0),
                observation_id=None if response.no_observation else observation_id,
                event_id=len(calls),
                status=response.status,
                reason=response.reason,
            )

        def get_release(self, oid):
            assert oid == observation_id
            calls.append(("get_release", oid))
            return record

        def close(self):
            closed.append("releases")

    monkeypatch.setattr(h, "DurableStore", Targets, raising=False)
    monkeypatch.setattr(h, "ReleaseStore", Releases, raising=False)
    return SimpleNamespace(
        calls=calls,
        closed=closed,
        record=record,
        target_id=target_id,
        observation_id=observation_id,
        response=response,
        outcomes=outcomes,
    )


def test_register_calls_public_stores_with_frozen_event_and_replays(
    registration_inputs, registration_stores
):
    from tests.f11_release_support import PROFILE

    ledger, registry_path, profile_path, release, identity = registration_inputs
    first = h.register_release(
        ledger, registry_path, "registry-key", profile_path, 1200, "host=unused"
    )
    # Replay must read original deadline, even if current parameters change.
    second = h.register_release(
        ledger, registry_path, "registry-key", profile_path, 1500, "host=unused"
    )
    assert first["outcome"] == "created" and second["outcome"] == "replayed"
    assert (
        first["observation_id"]
        == second["observation_id"]
        == str(registration_stores.observation_id)
    )
    assert first["released_at"] == second["released_at"] == "2026-10-14T10:00:00+00:00"
    assert first["deadline_at"] == second["deadline_at"] == "2026-10-14T11:00:00+00:00"
    requests = [v for k, v in registration_stores.calls if k == "record_release"]
    assert len(requests) == 2
    for request in requests:
        event = request["event"]
        assert (
            event.source == "u2-lab-configuration-patch"
            and event.source_release_id == release["release_id"]
        )
        assert event.released_at == h.utc(release["released_at"])
        assert event.before.model_dump(mode="json") == release["before_revision"]
        assert event.after.model_dump(mode="json") == release["after_revision"]
        assert event.auxiliary.deployment_uid == "dep"
        assert (
            request["identity"] == identity
            and request["target_id"] == registration_stores.target_id
        )
        assert request["health_profile"] == profile_path.read_text()
        assert request["health_profile_revision"] == PROFILE.revision
        assert request["actor"] == "u2-harness"
        assert (
            request["raw_body"]
            == h.canonical_json(event.model_dump(mode="json")).encode()
        )
    assert requests[0]["raw_body"] == requests[1]["raw_body"]
    assert (
        requests[0]["min_tracking_seconds"] == 1200
        and requests[1]["min_tracking_seconds"] == 1500
    )
    assert registration_stores.closed == ["releases", "targets"] * 2
    assert h.read_json(ledger / "registration.json")["outcome"] == "replayed"
    assert len(list((ledger / "registration-attempts").glob("*/receipt.json"))) == 2


@pytest.mark.parametrize(
    "broken", ["after_revision", "released_at", "template_digest", "registry_key"]
)
def test_register_missing_or_conflicting_identity_refuses_before_writes(
    registration_inputs, registration_stores, broken
):
    ledger, registry_path, profile_path, release, _ = registration_inputs
    resource_uid = "registry-key"
    if broken == "template_digest":
        release["after_revision"]["pod_template_digest"] = "sha256:" + "b" * 64
    elif broken == "registry_key":
        resource_uid = "not-registered"
    else:
        release.pop(broken)
    h.write_json(ledger / "release.json", release)
    with pytest.raises((ValueError, KeyError)):
        h.register_release(
            ledger, registry_path, resource_uid, profile_path, 1200, "unused"
        )
    assert not registration_stores.calls


@pytest.mark.parametrize(
    "outcome,status,reason,no_observation",
    [
        ("rejected", None, "TARGET_MISMATCH", True),
        ("created", "unknown", "late_delivery", False),
    ],
)
def test_register_preserves_product_refusal_and_late_delivery_receipts(
    registration_inputs, registration_stores, outcome, status, reason, no_observation
):
    ledger, registry_path, profile_path, _, _ = registration_inputs
    registration_stores.outcomes[0] = outcome
    registration_stores.response.status = status
    registration_stores.response.reason = reason
    registration_stores.response.no_observation = no_observation
    result = h.register_release(
        ledger, registry_path, "registry-key", profile_path, 1200, "host=unused"
    )
    assert (result["outcome"], result["status"], result["reason"]) == (
        outcome,
        status,
        reason,
    )
    assert h.read_json(ledger / "registration.json") == result
    if no_observation:
        assert result["released_at"] is None and result["deadline_at"] is None
        assert all(name != "get_release" for name, _ in registration_stores.calls)
    else:
        assert result["deadline_at"] == "2026-10-14T11:00:00+00:00"


def test_register_cli_uses_reserved_lab_dsn_and_prints_receipt(
    registration_inputs, registration_stores, monkeypatch, capsys
):
    ledger, registry_path, profile_path, _, _ = registration_inputs
    monkeypatch.setenv("OPSPILOT_LAB_DSN", "host=unused")
    h.occupation("claim", "register-test", "batch", h.utc_now() + timedelta(minutes=5))
    assert (
        h.main(
            [
                "--holder",
                "register-test",
                "--batch",
                "batch",
                "register",
                "--run",
                ledger.name,
                "--registry",
                str(registry_path),
                "--resource-uid",
                "registry-key",
                "--profile",
                str(profile_path),
                "--minimum",
                "1200",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["outcome"] == "created"


def test_register_canonicalizes_profile_and_preserves_original_bytes(
    registration_inputs, registration_stores
):
    from tests.f11_release_support import PROFILE

    ledger, registry_path, profile_path, _, _ = registration_inputs
    original = (
        "\n" + json.dumps(json.loads(profile_path.read_bytes()), indent=2) + "\n"
    ).encode()
    profile_path.write_bytes(original)
    canonical = h.canonical_content(PROFILE)
    assert original != canonical.encode()

    result = h.register_release(
        ledger, registry_path, "registry-key", profile_path, 1200, "host=unused"
    )

    request = next(
        value for name, value in registration_stores.calls if name == "record_release"
    )
    assert request["health_profile"] == canonical
    assert request["health_profile_revision"] == PROFILE.revision
    assert result["profile_original_sha256"] == sha256(original)
    assert result["outcome"] == "created"
    assert profile_path.read_bytes() == original
    attempts = list((ledger / "registration-attempts").iterdir())
    assert len(attempts) == 1
    assert (attempts[0] / "profile-original.json").read_bytes() == original
    assert h.read_json(attempts[0] / "receipt.json")[
        "profile_original_sha256"
    ] == sha256(original)


@pytest.fixture
def shared_worktree_harnesses(monkeypatch, tmp_path):
    import os
    import sys
    import types
    from pathlib import Path
    from types import SimpleNamespace

    main_root = tmp_path / "main"
    common = main_root / ".git"
    main_root.mkdir()
    roots = [tmp_path / "worktree-a", tmp_path / "worktree-b"]
    source = Path(h.__file__).read_text()
    monkeypatch.delenv("OPSPILOT_LAB_LEDGERS", raising=False)

    def git(command, **kwargs):
        assert command[0] == "git" and command[-2:] == ["rev-parse", "--git-common-dir"]
        assert kwargs["timeout"] > 0
        root = Path(command[command.index("-C") + 1])
        assert root in roots
        return SimpleNamespace(
            returncode=0, stdout=os.path.relpath(common, root).encode(), stderr=b""
        )

    monkeypatch.setattr(h.subprocess, "run", git)
    modules = []
    for i, root in enumerate(roots):
        module = types.ModuleType(f"_u2_worktree_test_{i}")
        module.__file__ = str(root / "scripts/f11_u2_harness.py")
        monkeypatch.setitem(sys.modules, module.__name__, module)
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        modules.append(module)
    return *modules, main_root / "tmp/lab-ledgers"


def test_different_worktrees_share_reservation_and_refuse_second_claim(
    shared_worktree_harnesses, capsys
):
    first, second, shared = shared_worktree_harnesses
    args = ["occupation", "claim", "--until", "2099-01-01T00:00:00Z"]
    assert first.main(["--holder", "alice", "--batch", "first", *args]) == 0
    assert second.main(["--holder", "bob", "--batch", "second", *args]) == 2
    assert first.LEDGERS == second.LEDGERS == shared
    assert first.read_json(shared / "occupation.json")["holder"] == "alice"
    assert "LAB_OCCUPIED" in capsys.readouterr().err


def test_worktrees_share_guards_and_pending_fault_record(shared_worktree_harnesses):
    first, second, shared = shared_worktree_harnesses
    with first.local_guard("publish", blocking=False):
        with pytest.raises(ValueError, match="LAB_OPERATION_IN_PROGRESS"):
            with second.local_guard("publish", blocking=False):
                pytest.fail("second worktree acquired the same guard")
    first.write_json(shared / "fault-active.json", {})
    with pytest.raises(ValueError, match="FAULT_RECOVERY_REQUIRED"):
        second.publish(shared / "next-run", "next-release")


def test_fresh_worktree_dry_run_does_not_resolve_git_or_create_shared_state(
    shared_worktree_harnesses, monkeypatch
):
    first, _, shared = shared_worktree_harnesses

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run invoked an external command")

    monkeypatch.setattr(first.subprocess, "run", forbidden)
    assert (
        first.main(["--dry-run", "release", "--run", "dry", "--release-id", "dry"]) == 0
    )
    assert first.LEDGERS is None and not shared.exists()


def test_lab_ledgers_absolute_override_never_invokes_git(monkeypatch, tmp_path):
    destination = tmp_path / "explicit-shared"
    monkeypatch.setenv("OPSPILOT_LAB_LEDGERS", str(destination))
    assert h.resolve_ledger_root(tmp_path / "one") == destination
    assert h.resolve_ledger_root(tmp_path / "two") == destination


@pytest.mark.parametrize("value", ["", "relative/path"])
def test_lab_ledgers_override_refuses_worktree_relative_paths(
    monkeypatch, tmp_path, value
):
    monkeypatch.setenv("OPSPILOT_LAB_LEDGERS", value)
    with pytest.raises(ValueError, match="ABSOLUTE_PATH_REQUIRED"):
        h.resolve_ledger_root(tmp_path)


def test_fold_merges_concurrent_rollout_and_fault_events_without_changing_bounds(
    monkeypatch, tmp_path, capsys
):
    ledger = h.ledger_path("merged-timeline")
    h.write_json(ledger / "context.json", _context().model_dump(mode="json"))
    truth = tmp_path / "truth.json"
    h.write_json(truth, {})
    monkeypatch.setattr(
        h, "external", lambda command, **kw: b"head" if "rev-parse" in command else b""
    )

    def at(second):
        return f"2026-10-14T10:00:{second:02d}+00:00"

    rollout = [
        dict(kind="new_pod_ready", observed_at=at(10), event_at=at(9)),
        dict(kind="old_pod_gone", lower_bound=at(3), upper_bound=at(8), event_at=None),
        dict(
            kind="release_patch_requested", at=at(0), source="original-release-source"
        ),
        dict(kind="rollout_tie", at=at(6)),
    ]
    fault = [
        dict(
            kind="injection_config_verified",
            request_before=at(2),
            request_after=at(4),
            planned_at=at(0),
        ),
        dict(
            kind="flagd_variant_effective",
            lower_bound=at(3),
            upper_bound=at(5),
            effective_at=at(5),
        ),
        dict(kind="restore_config_verified", at=at(6)),
    ]
    for filename, events in (("timeline.jsonl", rollout), ("fault.jsonl", fault)):
        for event in events:
            h.append_event(ledger / filename, event)
    original_files = {
        name: (ledger / name).read_bytes() for name in ("timeline.jsonl", "fault.jsonl")
    }
    assert (
        h.main(
            [
                "fold",
                "--run",
                "merged-timeline",
                "--minimum",
                "600",
                "--scenario",
                "probe",
                "--truth",
                str(truth),
            ]
        )
        == 0
    )
    capsys.readouterr()
    timeline = h.read_json(ledger / "summary.json")["timeline"]
    assert [e["kind"] for e in timeline] == [
        "release_patch_requested",
        "injection_config_verified",
        "flagd_variant_effective",
        "old_pod_gone",
        "restore_config_verified",
        "rollout_tie",
        "new_pod_ready",
    ]
    originals = {event["kind"]: event for event in rollout + fault}
    for event in timeline:
        assert {k: v for k, v in event.items() if k != "timeline_source"} == originals[
            event["kind"]
        ]
    assert [e["timeline_source"] for e in timeline] == [
        "timeline.jsonl",
        "fault.jsonl",
        "fault.jsonl",
        "timeline.jsonl",
        "fault.jsonl",
        "timeline.jsonl",
        "timeline.jsonl",
    ]
    assert all(
        (ledger / name).read_bytes() == raw for name, raw in original_files.items()
    )
