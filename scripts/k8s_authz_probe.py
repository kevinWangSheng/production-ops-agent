"""Record what the lab's read-only cluster identity can and cannot do (A1,
#553; decision table r3 A1-9, A1-10); never repairs the lab.

Standard library only, so the same file runs on the host and, passed as
``python -c``, inside the worker container (whose image has no ``scripts/``;
``--known-secrets -`` then reads the values from stdin):

    python3 scripts/k8s_authz_probe.py --env-file tmp/m1-kind-lab/k8s-investigator.env \\
        --known-secrets <file> --out-dir <dir>
    docker exec -i <worker> python -c "$(cat scripts/k8s_authz_probe.py)" \\
        --env-file /run/secrets/worker_env --side worker --known-secrets - \\
        < <file> > probe-worker.json

Credentials come only from the env file's ``OPSPILOT_K8S_API_URL``,
``OPSPILOT_K8S_TOKEN`` and ``OPSPILOT_K8S_CA_FILE`` lines (parsed, never
executed). Requests go straight to the API server: no proxy, no redirects.
Every write-class request carries ``dryRun=All``, so a wrong authorization
still changes nothing in the lab. Response bodies are never recorded (only
statuses, discovered names, rule lists and counts), and the output is checked
before it is written or printed: if it holds the token, a known lab secret
value or anything shaped like a JWT, nothing is written and the exit code is
2.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ENV_KEYS = ("OPSPILOT_K8S_API_URL", "OPSPILOT_K8S_TOKEN", "OPSPILOT_K8S_CA_FILE")
IDENTITY_NAMESPACE = "opspilot-readonly"
SERVICE_ACCOUNT = "opspilot-investigator"
#: The application namespace and the fault hook's switch ConfigMap (Q5).
DEFAULT_NAMESPACE = "otel-demo"
#: The host-form env file ``kind_lab.py up`` / ``k8s-token`` writes, from the
#: repository root (instance 0).
DEFAULT_ENV_FILE = Path("tmp/m1-kind-lab/k8s-investigator.env")
FLAG_CONFIGMAP = "flagd-config"
TIMEOUT_SECONDS = 15
MAX_BYTES = 8 * 1024 * 1024
PAGE_LIMIT = 500
#: Base64url JSON header of a JWT (``{"`` encodes to ``eyJ``), two segments.
JWT_SHAPE = re.compile(
    r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"
    # or any three dot-joined base64url runs of 8+ (header.payload.signature)
    r"|[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
)
#: Unregistered custom-resource groups (Q6): denied, installed or not.
CUSTOM_RESOURCES = (
    "/apis/argoproj.io/v1alpha1/applications",
    "/apis/source.toolkit.fluxcd.io/v1/gitrepositories",
    "/apis/networking.istio.io/v1beta1/virtualservices",
    "/apis/chaos-mesh.org/v1alpha1/podchaos",
    "/apis/monitoring.coreos.com/v1/prometheusrules",
)

#: ``(method, path, headers, body) -> (status, body)``; status 0 = no answer.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class RefusedOutput(Exception):
    """The output would carry the token, a known secret or a JWT shape."""


def read_env(path: Path) -> dict[str, str]:
    """The three ``OPSPILOT_K8S_*`` values: per key the first ``KEY=value``
    line wins, stripped, one pair of matching quotes removed (as the image
    entrypoint reads ``worker_env``)."""
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in ENV_KEYS or key in values:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    missing = [k for k in ENV_KEYS if not values.get(k)]
    if missing:
        raise ValueError(f"env file lacks {', '.join(missing)}")
    if urllib.parse.urlsplit(values["OPSPILOT_K8S_API_URL"]).scheme != "https":
        raise ValueError("OPSPILOT_K8S_API_URL is not https")
    return values


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _opener(ca_file: str | None) -> urllib.request.OpenerDirector:
    """Only the lab CA; no proxy (``ProxyHandler({})`` replaces the
    environment-reading default) and no redirects."""
    context = ssl.create_default_context(cafile=ca_file)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirect(),
    )


def _open(
    opener: urllib.request.OpenerDirector, req: urllib.request.Request
) -> tuple[int, bytes]:
    try:
        with opener.open(req, timeout=TIMEOUT_SECONDS) as response:
            return int(response.status), response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


def https_transport(api_url: str, token: str, ca_file: str) -> Transport:
    opener = _opener(ca_file)
    base = api_url.rstrip("/")

    def send(
        method: str, path: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(
            base + path,
            data=body,
            headers={"Authorization": f"Bearer {token}", **headers},
            method=method,
        )
        return _open(opener, req)

    return send


def authorization(env_file: Path) -> str:
    """The ``Authorization`` header value for the env file's token."""
    return "Bearer " + read_env(env_file)["OPSPILOT_K8S_TOKEN"]


_OPENERS: dict[str, urllib.request.OpenerDirector] = {}


def request(
    method: str,
    path: str,
    auth: str,
    *,
    env_file: Path,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes]:
    """One request to the env file's API server; ``(0, b"")`` when nothing
    answered. Never raises on an HTTP or transport error."""
    env = read_env(env_file)
    ca_file = env["OPSPILOT_K8S_CA_FILE"]
    if ca_file not in _OPENERS:
        _OPENERS[ca_file] = _opener(ca_file)
    req = urllib.request.Request(
        env["OPSPILOT_K8S_API_URL"].rstrip("/") + path,
        data=body,
        headers={**(headers or {}), "Authorization": auth},
        method=method,
    )
    return _open(_OPENERS[ca_file], req)


def _json(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body) if len(body) <= MAX_BYTES else None
    except ValueError:
        value = None
    return value if isinstance(value, dict) else {}


def discover(send: Transport) -> dict[str, str | None]:
    """A kube-system pod (with its first container) and a node to aim the
    log, exec, attach and proxy rows at; ``None`` when the list is denied."""
    found: dict[str, str | None] = {"pod": None, "container": None, "node": None}
    status, body = send("GET", "/api/v1/namespaces/kube-system/pods?limit=1", {}, None)
    items = _json(body).get("items") if status == 200 else None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        found["pod"] = str(items[0].get("metadata", {}).get("name") or "") or None
        containers = items[0].get("spec", {}).get("containers") or []
        if containers and isinstance(containers[0], dict):
            found["container"] = str(containers[0].get("name") or "") or None
    status, body = send("GET", "/api/v1/nodes?limit=1", {}, None)
    items = _json(body).get("items") if status == 200 else None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        found["node"] = str(items[0].get("metadata", {}).get("name") or "") or None
    return found


def _q(value: str | None) -> str:
    # an undiscovered name still yields a well-formed path (and a mismatch
    # on the rows that need a real object)
    return urllib.parse.quote(value or "undiscovered", safe="")


def rows(namespace: str, found: dict[str, str | None]) -> list[dict[str, Any]]:
    """The probe table: ``name``, ``method``, ``path``, ``expected`` and,
    for some, request ``headers`` and a JSON ``body``."""
    ns = _q(namespace)
    pod = f"/api/v1/namespaces/kube-system/pods/{_q(found['pod'])}"
    log_query = "?tailLines=1" + (
        f"&container={_q(found['container'])}" if found["container"] else ""
    )
    dry = "dryRun=All"
    allowed = [
        ("list namespaces", "GET", "/api/v1/namespaces"),
        ("list nodes", "GET", "/api/v1/nodes"),
        ("list pods, all namespaces", "GET", "/api/v1/pods"),
        ("list kube-system pods", "GET", "/api/v1/namespaces/kube-system/pods"),
        ("list events (core)", "GET", "/api/v1/events"),
        ("list events (events.k8s.io)", "GET", "/apis/events.k8s.io/v1/events"),
        ("list configmaps, all namespaces", "GET", "/api/v1/configmaps"),
        (
            "get the fault switch ConfigMap (Q5)",
            "GET",
            f"/api/v1/namespaces/{ns}/configmaps/{FLAG_CONFIGMAP}",
        ),
        ("list deployments", "GET", "/apis/apps/v1/deployments"),
        ("list replicasets", "GET", "/apis/apps/v1/replicasets"),
        ("list services", "GET", "/api/v1/services"),
        ("read a kube-system pod log", "GET", pod + "/log" + log_query),
    ]
    table: list[dict[str, Any]] = [
        {"name": n, "method": m, "path": p, "expected": 200} for n, m, p in allowed
    ]
    denied: list[tuple[str, str, str, dict[str, str], Any]] = [
        ("list secrets, all namespaces", "GET", "/api/v1/secrets", {}, None),
        (
            "list kube-system secrets",
            "GET",
            "/api/v1/namespaces/kube-system/secrets",
            {},
            None,
        ),
        ("list app secrets", "GET", f"/api/v1/namespaces/{ns}/secrets", {}, None),
        ("watch pods", "GET", "/api/v1/pods?watch=true", {}, None),
        (
            "exec (GET)",
            "GET",
            pod + "/exec?command=true&stdout=true",
            {},
            None,
        ),
        (
            "exec (POST)",
            "POST",
            # a connect request has no dry run; authorization refuses first
            pod + f"/exec?command=true&stdout=true&{dry}",
            {},
            None,
        ),
        ("attach", "GET", pod + "/attach?stdout=true", {}, None),
        ("port-forward", "GET", pod + "/portforward?ports=80", {}, None),
        ("pod proxy", "GET", pod + "/proxy/", {}, None),
        (
            "node proxy",
            "GET",
            f"/api/v1/nodes/{_q(found['node'])}/proxy/healthz",
            {},
            None,
        ),
        (
            "service proxy",
            "GET",
            "/api/v1/namespaces/default/services/https:kubernetes:443/proxy/",
            {},
            None,
        ),
        (
            "issue itself a token (dry run)",
            "POST",
            f"/api/v1/namespaces/{IDENTITY_NAMESPACE}/serviceaccounts/"
            f"{SERVICE_ACCOUNT}/token?{dry}",
            {},
            {
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenRequest",
                "spec": {"expirationSeconds": 600},
            },
        ),
        (
            "impersonate a user",
            "GET",
            "/api/v1/namespaces",
            {"Impersonate-User": "kubernetes-admin"},
            None,
        ),
        (
            "impersonate a group",
            "GET",
            "/api/v1/namespaces",
            {"Impersonate-User": "probe", "Impersonate-Group": "system:masters"},
            None,
        ),
        (
            "create a ConfigMap (dry run)",
            "POST",
            f"/api/v1/namespaces/{ns}/configmaps?{dry}",
            {},
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "opspilot-a1-probe"},
                "data": {"probe": "a1"},
            },
        ),
        (
            "patch the fault switch ConfigMap (dry run)",
            "PATCH",
            f"/api/v1/namespaces/{ns}/configmaps/{FLAG_CONFIGMAP}?{dry}",
            {"Content-Type": "application/merge-patch+json"},
            {"metadata": {"labels": {"opspilot-a1-probe": "1"}}},
        ),
        ("delete a pod (dry run)", "DELETE", pod + f"?{dry}", {}, None),
        (
            "scale a deployment (dry run)",
            "PATCH",
            f"/apis/apps/v1/namespaces/{ns}/deployments/checkout/scale?{dry}",
            {"Content-Type": "application/merge-patch+json"},
            {"spec": {"replicas": 0}},
        ),
        (
            "create a namespace (dry run)",
            "POST",
            f"/api/v1/namespaces?{dry}",
            {},
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": "opspilot-a1-probe"},
            },
        ),
        (
            "bind itself cluster-admin (dry run)",
            "POST",
            f"/apis/rbac.authorization.k8s.io/v1/clusterrolebindings?{dry}",
            {},
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": "opspilot-a1-probe"},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": "cluster-admin",
                },
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": SERVICE_ACCOUNT,
                        "namespace": IDENTITY_NAMESPACE,
                    }
                ],
            },
        ),
    ] + [
        (f"list custom resource {path.split('/')[2]}", "GET", path, {}, None)
        for path in CUSTOM_RESOURCES
    ]
    for name, method, path, headers, body in denied:
        row: dict[str, Any] = {
            "name": name,
            "method": method,
            "path": path,
            "expected": 403,
        }
        if headers:
            row["headers"] = headers
        if body is not None:
            row["body"] = body
        table.append(row)
    return table


def _send_row(send: Transport, row: dict[str, Any]) -> int:
    headers = dict(row.get("headers", {}))
    body = None
    if "body" in row:
        headers.setdefault("Content-Type", "application/json")
        body = json.dumps(row["body"]).encode()
    status, _ = send(row["method"], row["path"], headers, body)
    return status


def configmap_scan(send: Transport, needles: list[str]) -> dict[str, Any]:
    """Search every ConfigMap's ``data`` and ``binaryData`` (decoded) of the
    whole cluster for the known lab secret values; records counts and the
    ``namespace/name/key`` of a hit, never a value."""
    hits: list[str] = []
    scanned = 0
    token = ""
    complete = True
    while True:
        path = f"/api/v1/configmaps?limit={PAGE_LIMIT}"
        if token:
            path += "&continue=" + urllib.parse.quote(token, safe="")
        status, body = send("GET", path, {}, None)
        page = _json(body) if status == 200 else {}
        items = page.get("items")
        if not isinstance(items, list):
            complete = False
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            scanned += 1
            meta = item.get("metadata") or {}
            where = f"{meta.get('namespace')}/{meta.get('name')}"
            fields: dict[str, str] = {}
            for key, value in (item.get("data") or {}).items():
                fields[key] = str(value)
            for key, value in (item.get("binaryData") or {}).items():
                try:
                    fields[key] = base64.b64decode(str(value)).decode(
                        "utf-8", "replace"
                    )
                except ValueError:
                    fields[key] = str(value)
            for key, text in fields.items():
                if any(n and n in text for n in needles):
                    hits.append(f"{where}/{key}")
        token = str((page.get("metadata") or {}).get("continue") or "")
        if not token:
            break
    return {
        "configmaps_scanned": scanned,
        "known_values": len([n for n in needles if n]),
        "hits": len(hits),
        "hit_keys": hits,
        "expected": 0,
        "matches": complete and not hits and scanned > 0,
    }


def rules_reviews(send: Transport, namespaces: tuple[str, ...]) -> dict[str, Any]:
    """SelfSubjectRulesReview per namespace (create-only, not stored)."""
    reviews: dict[str, Any] = {}
    for namespace in namespaces:
        body = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectRulesReview",
            "spec": {"namespace": namespace},
        }
        status, answer = send(
            "POST",
            "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews",
            {"Content-Type": "application/json"},
            json.dumps(body).encode(),
        )
        review = _json(answer).get("status") or {}
        reviews[namespace] = {
            "status": status,
            "resourceRules": review.get("resourceRules"),
            "nonResourceRules": review.get("nonResourceRules"),
            "incomplete": review.get("incomplete"),
        }
    return reviews


def _sender(env_file: Path, auth: str) -> Transport:
    def send(
        method: str, path: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return request(
            method, path, auth, env_file=env_file, headers=headers, body=body
        )

    return send


def probe(
    env_file: Path = DEFAULT_ENV_FILE,
    *,
    namespace: str = DEFAULT_NAMESPACE,
    known: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """The probe table and the ConfigMap search, through ``request``; the
    report's ``ok`` also needs known lab secret values to have been given.
    Nothing here retries or repairs."""
    auth = authorization(env_file)
    send = _sender(env_file, auth)
    needles = [auth.removeprefix("Bearer "), *known]
    found = discover(send)
    results = []
    for row in rows(namespace, found):
        status = _send_row(send, row)
        results.append(
            {
                "name": row["name"],
                "method": row["method"],
                "path": row["path"],
                "status": status,
                "expected": row["expected"],
                "matches": status == row["expected"],
            }
        )
    scan = configmap_scan(send, needles)
    return {
        "discovered": found,
        "requests": results,
        "configmap_secret_scan": scan,
        "known_secrets_supplied": bool(known),
        "ok": all(r["matches"] for r in results) and scan["matches"] and bool(known),
    }


def check_output(text: str, token: str, known: list[str]) -> None:
    """Raise ``RefusedOutput`` if ``text`` holds the token (or a long piece
    of it), a known secret value or a JWT-shaped string."""
    pieces = [token] + [p for p in re.split(r"[.]", token) if len(p) >= 16]
    if any(p and p in text for p in pieces):
        raise RefusedOutput("token")
    if any(k and k in text for k in known):
        raise RefusedOutput("known secret value")
    if JWT_SHAPE.search(text):
        raise RefusedOutput("JWT shape")


def read_known(path: Path) -> list[str]:
    """One value per line; ``-`` reads stdin."""
    text = sys.stdin.read() if str(path) == "-" else path.read_text()
    return [line.strip() for line in text.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--known-secrets",
        type=Path,
        help="lab secret values to search the cluster's ConfigMaps for, one per "
        "line (- for stdin); without it the report is not ok",
    )
    parser.add_argument("--side", choices=("host", "worker"), default="host")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--manifest", type=Path, help="RBAC manifest to fingerprint")
    parser.add_argument("--output", type=Path, help="write the probe report here")
    parser.add_argument(
        "--rules-output", type=Path, help="write the SelfSubjectRulesReviews here"
    )
    parser.add_argument(
        "--out-dir", type=Path, help="write probe.json and rules.json here"
    )
    args = parser.parse_args(argv)
    try:
        env = read_env(args.env_file)
        known = read_known(args.known_secrets) if args.known_secrets else []
    except (OSError, ValueError):
        print(json.dumps({"ok": False, "error": "cannot read the probe inputs"}))
        return 1
    token = env["OPSPILOT_K8S_TOKEN"]
    report = probe(env_file=args.env_file, namespace=args.namespace, known=known)
    report = {
        "side": args.side,
        "api_url": env["OPSPILOT_K8S_API_URL"],
        "service_account": f"{IDENTITY_NAMESPACE}/{SERVICE_ACCOUNT}",
        **(
            {"manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest()}
            if args.manifest
            else {}
        ),
        **report,
    }
    targets: dict[Path, str] = {}
    probe_text = json.dumps(report, indent=2) + "\n"
    rules_paths = [p for p in (args.rules_output,) if p] + (
        [args.out_dir / "rules.json"] if args.out_dir else []
    )
    if args.output:
        targets[args.output] = probe_text
    if args.out_dir:
        targets[args.out_dir / "probe.json"] = probe_text
    if rules_paths:
        sender = _sender(args.env_file, authorization(args.env_file))
        rules = rules_reviews(sender, (args.namespace, "kube-system"))
        rules_text = json.dumps({"side": args.side, "reviews": rules}, indent=2) + "\n"
        targets.update({path: rules_text for path in rules_paths})
    try:
        for text in [probe_text, *targets.values()]:
            check_output(text, token, known)
    except RefusedOutput as exc:
        print(json.dumps({"ok": False, "error": f"output refused: {exc}"}))
        return 2
    for path, text in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if targets:
        print(json.dumps({"ok": report["ok"], "written": [str(p) for p in targets]}))
    else:
        print(probe_text, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
