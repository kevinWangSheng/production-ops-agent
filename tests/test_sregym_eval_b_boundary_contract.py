"""#667 PR-B contract tests: ports and the S-5 read-only boundary checks.

Written from docs/tasks/2026-11-19-sregym-667.md, section "PR-B 公开接口"
(including the Loki read-only gateway revision), and pre-review contract-test
groups 5, 6 and 7, without reading the PR-B implementation.

* Layer 1: ``check_rbac_manifest`` passes the real lab manifest and reports
  each mutation (write verbs, secrets, exec/attach/portforward, token,
  ``/proxy``, wildcards, no role at all).
* Layer 2: ``CAN_I_CHECKS`` holds the required probes; ``can_i_verdict``
  accepts only ``yes``/``no`` after stripping whitespace; anything else, and a
  missing answer, is a mismatch.
* Layer 3: ``check_product_env`` refuses forbidden key prefixes, judge keys,
  forbidden values, web-only secrets and a Loki URL that bypasses the gateway.
* ``problem_id_digest`` / ``check_no_problem_id_digest``: the entry only holds
  a digest and still catches spelling variants of the problem ID.
"""

from __future__ import annotations

import hashlib

import pytest

from tests.sregym_eval_support import REPO, code_of, mod

MANIFEST = REPO / "scripts" / "kind_lab" / "opspilot-readonly-rbac.yaml"
SUBJECT = "system:serviceaccount:opspilot-readonly:opspilot-investigator"

# --- ports -----------------------------------------------------------------------------------

PORTS = {
    "pg": 29510,
    "web": 29580,
    "prometheus": 29590,
    "jaeger": 29586,
    "loki": 29531,
    "loki_gateway": 29532,
    "unreachable": 29599,
    "conductor": 28000,
    "mcp": 29954,
    "sregym_proxy": 26443,
    "opspilot_proxy": 26444,
}


def test_ports_defaults_are_the_assigned_numbers() -> None:
    ports = mod("ports").Ports()
    for name, number in PORTS.items():
        assert getattr(ports, name) == number, name


def test_ports_is_frozen() -> None:
    ports = mod("ports").Ports()
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError or AttributeError
        ports.web = 1  # type: ignore[misc]


def test_per_trial_ports_are_every_port_except_unreachable_and_gateway() -> None:
    # interface revision 0045f5ec: the Loki gateway stays up for the whole round
    per_trial = mod("ports").Ports().per_trial()
    assert isinstance(per_trial, dict)
    expected = {
        n for name, n in PORTS.items() if name not in ("unreachable", "loki_gateway")
    }
    assert set(per_trial.values()) == expected
    assert all(isinstance(v, int) for v in per_trial.values())


# --- layer 1: RBAC manifest ------------------------------------------------------------------

ROLE = """\
apiVersion: rbac.authorization.k8s.io/v1
kind: {kind}
metadata:
  name: test-role
{ns}rules:
  - apiGroups: [{groups}]
    resources: [{resources}]
    verbs: [{verbs}]
"""


def _role(
    *,
    kind: str = "ClusterRole",
    groups: str = '""',
    resources: str = "pods",
    verbs: str = "get, list",
) -> str:
    ns = "  namespace: default\n" if kind == "Role" else ""
    return ROLE.format(
        kind=kind, ns=ns, groups=groups, resources=resources, verbs=verbs
    )


def _check(text: str) -> list[str]:
    return mod("boundary").check_rbac_manifest(text)


def test_allowed_verbs_and_forbidden_resources_constants() -> None:
    b = mod("boundary")
    assert set(b.RBAC_ALLOWED_VERBS) == {"get", "list"}
    forbidden = set(b.RBAC_FORBIDDEN_RESOURCES)
    for r in (
        "secrets",
        "pods/exec",
        "pods/attach",
        "pods/portforward",
        "serviceaccounts/token",
        "*",
    ):
        assert r in forbidden, r


def test_real_lab_manifest_passes() -> None:
    assert _check(MANIFEST.read_text()) == []


def test_minimal_readonly_role_passes() -> None:
    assert _check(_role()) == []
    assert _check(_role(kind="Role")) == []


@pytest.mark.parametrize(
    "verb", ["create", "patch", "delete", "update", "watch", "impersonate"]
)
def test_any_verb_other_than_get_list_is_reported(verb: str) -> None:
    found = _check(_role(verbs=f"get, list, {verb}"))
    assert f"VERB:{verb}" in found


@pytest.mark.parametrize(
    "resource",
    [
        "secrets",
        "pods/exec",
        "pods/attach",
        "pods/portforward",
        "serviceaccounts/token",
        "services/proxy",
        "nodes/proxy",
    ],
)
def test_forbidden_resources_are_reported(resource: str) -> None:
    found = _check(_role(resources=f"pods, {resource}"))
    assert f"RESOURCE:{resource}" in found


def test_forbidden_resource_in_namespaced_role_is_reported() -> None:
    assert "RESOURCE:secrets" in _check(_role(kind="Role", resources="secrets"))


def test_wildcard_verbs_resources_and_groups_are_reported() -> None:
    assert _check(_role(verbs='"*"')) != []
    assert _check(_role(resources='"*"')) != []
    assert "WILDCARD:apiGroups" in _check(_role(groups='"*"'))


def test_violation_in_a_later_document_of_the_real_manifest_is_reported() -> None:
    text = MANIFEST.read_text().rstrip() + "\n---\n" + _role(resources="secrets")
    assert "RESOURCE:secrets" in _check(text)


def test_mutating_the_real_manifest_reports_the_mutation() -> None:
    text = MANIFEST.read_text()
    mutated = text.replace("verbs: [get, list]", "verbs: [get, list, create]", 1)
    assert mutated != text
    assert "VERB:create" in _check(mutated)
    mutated = text.replace(
        "      - pods/log\n", "      - pods/log\n      - pods/exec\n", 1
    )
    assert mutated != text
    assert "RESOURCE:pods/exec" in _check(mutated)


def test_manifest_without_any_role_is_reported() -> None:
    text = """\
apiVersion: v1
kind: ServiceAccount
metadata:
  name: opspilot-investigator
  namespace: opspilot-readonly
"""
    assert "NO_ROLE" in _check(text)
    assert "NO_ROLE" in _check("")


# --- layer 2: can-i ----------------------------------------------------------------------------

MUST_NO = [
    ("create", "deployments.apps"),
    ("patch", "deployments.apps"),
    ("delete", "deployments.apps"),
    ("create", "pods"),
    ("patch", "pods"),
    ("delete", "pods"),
    ("create", "configmaps"),
    ("patch", "configmaps"),
    ("delete", "configmaps"),
    ("get", "secrets"),
    ("list", "secrets"),
    ("create", "pods/exec"),
    ("create", "pods/portforward"),
    ("create", "serviceaccounts/token"),
]
MUST_YES = [("list", "pods"), ("list", "deployments.apps"), ("list", "events")]


def _checks() -> tuple:
    return mod("boundary").CAN_I_CHECKS


def _find(verb: str, resource: str):  # noqa: ANN202
    for c in _checks():
        if (c.verb, c.resource) == (verb, resource):
            return c
    raise AssertionError(f"no can-i check {verb} {resource}")


def test_readonly_subject_constant() -> None:
    assert mod("boundary").READONLY_SUBJECT == SUBJECT


def test_can_i_checks_hold_every_required_probe() -> None:
    for verb, resource in MUST_NO:
        assert _find(verb, resource).expect == "no", (verb, resource)
    for verb, resource in MUST_YES:
        assert _find(verb, resource).expect == "yes", (verb, resource)
    assert all(c.expect in ("yes", "no") for c in _checks())


def test_no_write_verb_is_ever_expected_yes() -> None:
    writes = {"create", "patch", "delete", "update", "deletecollection"}
    for c in _checks():
        if c.verb in writes:
            assert c.expect == "no", c
        if c.resource == "secrets":
            assert c.expect == "no", c


def test_can_i_argv_plain_resource() -> None:
    argv = mod("boundary").can_i_argv(_find("list", "pods"), SUBJECT)
    assert argv[:5] == ["kubectl", "auth", "can-i", "list", "pods"]
    assert "--all-namespaces" in argv
    assert f"--as={SUBJECT}" in argv


def test_can_i_argv_subresource_uses_subresource_flag() -> None:
    argv = mod("boundary").can_i_argv(_find("create", "pods/exec"), SUBJECT)
    assert argv[:5] == ["kubectl", "auth", "can-i", "create", "pods"]
    assert "--subresource=exec" in argv
    assert "pods/exec" not in argv
    assert "--all-namespaces" in argv and f"--as={SUBJECT}" in argv


def _good_answers() -> dict:
    return {c: c.expect for c in _checks()}


def _verdict(answers: dict) -> list[str]:
    return mod("boundary").can_i_verdict(answers)


def test_all_expected_answers_pass() -> None:
    assert _verdict(_good_answers()) == []


def test_answers_with_surrounding_whitespace_pass() -> None:
    answers = {c: f"  {a}\n" for c, a in _good_answers().items()}
    assert _verdict(answers) == []


@pytest.mark.parametrize("verb, resource", MUST_NO)
def test_any_must_no_answered_yes_is_a_mismatch(verb: str, resource: str) -> None:
    answers = _good_answers()
    answers[_find(verb, resource)] = "yes"
    assert _verdict(answers) == [f"CAN_I:{verb}:{resource}:yes"]


def test_list_pods_answered_no_is_a_mismatch() -> None:
    answers = _good_answers()
    answers[_find("list", "pods")] = "no"
    assert _verdict(answers) == ["CAN_I:list:pods:no"]


@pytest.mark.parametrize(
    "answer",
    [
        "",
        "error: You must be logged in to the server (Unauthorized)",
        "maybe",
        "yes no",
    ],
)
def test_answers_other_than_yes_or_no_are_mismatches(answer: str) -> None:
    answers = _good_answers()
    answers[_find("create", "pods")] = answer
    found = _verdict(answers)
    assert len(found) == 1 and found[0].startswith("CAN_I:create:pods:")
    # an error answer for a must-yes check is a mismatch too
    answers = _good_answers()
    answers[_find("list", "pods")] = answer
    found = _verdict(answers)
    assert len(found) == 1 and found[0].startswith("CAN_I:list:pods:")


def test_missing_answer_is_a_mismatch() -> None:
    answers = _good_answers()
    del answers[_find("get", "secrets")]
    found = _verdict(answers)
    assert len(found) == 1 and found[0].startswith("CAN_I:get:secrets:")
    assert len(_verdict({})) == len(_checks())


# --- layer 3: product environment ---------------------------------------------------------------

ENGINEER_KUBECONFIG = "/home/eng/.kube/sregym-admin.yaml"
ENGINEER_TOKEN = "eng-admin-token-7f3a9c"
JUDGE_KEY = "judge-canary-value"
GATEWAY_URL = "http://127.0.0.1:29532"
LOKI_FORWARD = "127.0.0.1:29531"
FORBIDDEN_VALUES = (ENGINEER_KUBECONFIG, ENGINEER_TOKEN, JUDGE_KEY)


def _clean(role: str) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/eng",
        "OPSPILOT_TOOL_PROFILE": "otel-demo-ext",
        "OPSPILOT_PROMETHEUS_URL": "http://127.0.0.1:29590",
        "OPSPILOT_LOKI_URL": GATEWAY_URL,
    }
    if role == "worker":
        env.update(
            OPSPILOT_K8S_TOKEN="readonly-sa-token",
            DEEPSEEK_API_KEY="sk-deepseek-test",
            LANGSMITH_API_KEY="ls-test",
            OPSPILOT_LOKI_USERNAME="reader",
            OPSPILOT_LOKI_PASSWORD="reader-pass",
        )
    return env


def _env_check(env: dict[str, str], role: str) -> None:
    mod("boundary").check_product_env(
        env,
        role=role,
        forbidden_values=FORBIDDEN_VALUES,
        backend_addresses=(LOKI_FORWARD,),
    )


def _env_refused(env: dict[str, str], role: str) -> str:
    with pytest.raises(mod("boundary").BoundaryError) as info:
        _env_check(env, role)
    return code_of(info.value)


@pytest.mark.parametrize("role", ["web", "worker"])
def test_clean_product_env_passes(role: str) -> None:
    _env_check(_clean(role), role)


def test_forbidden_prefix_constants() -> None:
    b = mod("boundary")
    for p in (
        "KUBECONFIG",
        "JUDGE_",
        "SREGYM_",
        "MCP_SERVER_",
        "API_HOSTNAME",
        "API_PORT",
        "K8S_PROXY_",
        "AGENT_",
        "HARNESS_",
        "KIND_",
    ):
        assert p in tuple(b.FORBIDDEN_ENV_PREFIXES), p
    assert {"ZAI_API_KEY", "GLM_API_KEY"} <= set(b.JUDGE_KEY_NAMES)


@pytest.mark.parametrize("role", ["web", "worker"])
@pytest.mark.parametrize(
    "key",
    [
        "KUBECONFIG",
        "JUDGE_API_KEY",
        "JUDGE_API_BASE",
        "SREGYM_AGENT_REGISTRY",
        "SREGYM_EVAL_JOB",
        "MCP_SERVER_PORT",
        "MCP_SERVER_URL",
        "API_HOSTNAME",
        "API_PORT",
        "K8S_PROXY_PORT",
        "AGENT_LOGS_DIR",
        "HARNESS_MODE",
        "KIND_CLUSTER_NAME",
        "ZAI_API_KEY",
        "GLM_API_KEY",
    ],
)
def test_forbidden_keys_are_refused(role: str, key: str) -> None:
    env = _clean(role)
    env[key] = "x"
    assert _env_refused(env, role) == f"ENV_ISOLATION:{role}:{key}"


@pytest.mark.parametrize("role", ["web", "worker"])
@pytest.mark.parametrize("value", FORBIDDEN_VALUES)
def test_forbidden_values_anywhere_are_refused(role: str, value: str) -> None:
    env = _clean(role)
    env["OPSPILOT_SOME_SETTING"] = f"prefix:{value}:suffix"
    assert _env_refused(env, role) == f"ENV_ISOLATION:{role}:OPSPILOT_SOME_SETTING"


@pytest.mark.parametrize(
    "key",
    [
        "OPSPILOT_K8S_TOKEN",
        "DEEPSEEK_API_KEY",
        "OPSPILOT_LOKI_BEARER_TOKEN",
        "LANGSMITH_API_KEY",
    ],
)
def test_web_must_not_hold_worker_secrets(key: str) -> None:
    env = _clean("web")
    env[key] = "value"
    assert _env_refused(env, "web") == f"ENV_ISOLATION:web:{key}"


def test_worker_may_hold_its_own_secrets() -> None:
    _env_check(_clean("worker"), "worker")


@pytest.mark.parametrize("role", ["web", "worker"])
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:29531",
        "http://127.0.0.1:29531/",
        "http://127.0.0.1:29531/loki",
    ],
)
def test_loki_url_pointing_past_the_gateway_is_refused(role: str, url: str) -> None:
    env = _clean(role)
    env["OPSPILOT_LOKI_URL"] = url
    assert _env_refused(env, role) == f"ENV_ISOLATION:{role}:OPSPILOT_LOKI_URL"


def test_refusal_code_does_not_carry_the_value() -> None:
    env = _clean("worker")
    env["OPSPILOT_X"] = ENGINEER_TOKEN
    with pytest.raises(mod("boundary").BoundaryError) as info:
        _env_check(env, "worker")
    assert ENGINEER_TOKEN not in str(info.value)
    assert ENGINEER_TOKEN not in repr(info.value)


# --- problem ID digest ----------------------------------------------------------------------------

PID = "network_policy_block"
SQUASHED = "networkpolicyblock"


def test_problem_id_digest_normalises_like_pr_a() -> None:
    b = mod("boundary")
    d = b.problem_id_digest(PID)
    assert d.sha256 == hashlib.sha256(SQUASHED.encode()).hexdigest()
    assert d.length == len(SQUASHED)
    for variant in (
        "Network-Policy Block",
        "NETWORK_POLICY_BLOCK",
        "network policy\tblock",
    ):
        assert b.problem_id_digest(variant) == d


def test_digest_does_not_contain_the_problem_id() -> None:
    d = mod("boundary").problem_id_digest(PID)
    assert PID not in repr(d) and SQUASHED not in repr(d)


@pytest.mark.parametrize(
    "value",
    [
        PID,
        f"The issue is a {PID} in ns",
        "suspect Network Policy Block on checkout",
        "network-policy-block",
        "xxNETWORKPOLICYBLOCKyy",
    ],
)
def test_digest_check_catches_spellings_anywhere(value: str) -> None:
    b = mod("boundary")
    leak = mod("question").AnswerLeak
    with pytest.raises(leak) as info:
        b.check_no_problem_id_digest(
            b.problem_id_digest(PID), {"key": "sregym-r1-p03-t1", "question": value}
        )
    assert code_of(info.value) == "PROBLEM_ID_IN:question"


def test_digest_check_passes_clean_fields() -> None:
    b = mod("boundary")
    b.check_no_problem_id_digest(
        b.problem_id_digest(PID),
        {
            "question": "Application: social-network\nNamespace: social-network",
            "key": "sregym-r1-p03-t1",
            "empty": "",
            "near": "network policy blok",
        },
    )


def test_digest_check_does_not_flag_a_different_id_sharing_words() -> None:
    b = mod("boundary")
    d = b.problem_id_digest("duplicate_pvc_mounts_social_network")
    b.check_no_problem_id_digest(d, {"question": "Application: social_network"})
