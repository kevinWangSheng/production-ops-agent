#!/usr/bin/env bash
# Synthetic Helm release history for CI k8s-backends (B4, #563); the names
# are fixed by docs/tasks/2026-11-20-tool-expansion-b4.md ("CI job").
#
# Runs with the runner's admin kubeconfig (engineer side) from the
# repository root. Reads B4_HELM3_BIN and B4_HELM4_BIN. Prints names only,
# never a Secret body.
set -euo pipefail

: "${B4_HELM3_BIN:?Helm 3 binary}"
: "${B4_HELM4_BIN:?Helm 4 binary}"
chart=tests/fixtures/b4_helm/chart
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

kubectl create namespace b4-a
kubectl create namespace b4-b
kubectl create namespace b4-junk

# The same chart at 0.2.0 / 1.1.0 for the upgrade.
cp -R "$chart" "$work/chart-0.2.0"
sed -i 's/^version: 0.1.0$/version: 0.2.0/; s/^appVersion: "1.0.0"$/appVersion: "1.1.0"/' \
    "$work/chart-0.2.0/Chart.yaml"
grep -q '^version: 0.2.0$' "$work/chart-0.2.0/Chart.yaml"

# b4-a/shop, Helm 3: install, upgrade, failed upgrade, rollback to 1.
"$B4_HELM3_BIN" install shop "$chart" -n b4-a --set fromSet=CANARY-B4-SET \
    --labels team=CANARY-B4-LABEL --wait --timeout 120s
"$B4_HELM3_BIN" upgrade shop "$work/chart-0.2.0" -n b4-a --set fromSet=CANARY-B4-SET \
    --wait --timeout 120s
if "$B4_HELM3_BIN" upgrade shop "$work/chart-0.2.0" -n b4-a --set failName=true \
    --timeout 120s; then
    echo "the failing upgrade of shop succeeded" >&2
    exit 1
fi
"$B4_HELM3_BIN" rollback shop 1 -n b4-a --wait --timeout 120s
"$B4_HELM3_BIN" history shop -n b4-a

# b4-b/cart, Helm 4: install and 12 upgrades, keeping 10 revisions.
"$B4_HELM4_BIN" install cart "$chart" -n b4-b --set fromSet=CANARY-B4-SET \
    --labels team=CANARY-B4-LABEL --wait --timeout 120s
for n in $(seq 1 12); do
    "$B4_HELM4_BIN" upgrade cart "$chart" -n b4-b --history-max 10 \
        --set "fromSet=CANARY-B4-SET-$n" --wait --timeout 120s
done
"$B4_HELM4_BIN" history cart -n b4-b

# b4-junk: hand-made records of the Helm type (kubectl, not Helm).
python3 -I - "$work" <<'PY'
import base64, gzip, json, sys
from pathlib import Path

work = Path(sys.argv[1])

def release(name):
    return {
        "name": name,
        "namespace": "b4-junk",
        "version": 1,
        "info": {
            "first_deployed": "2026-11-20T09:00:00Z",
            "last_deployed": "2026-11-20T09:00:00Z",
            "description": "Install complete",
            "status": "deployed",
            "notes": "CANARY-B4-NOTES",
        },
        "chart": {
            "metadata": {"name": "b4-fixture", "version": "0.1.0", "appVersion": "1.0.0"},
            "values": {"image": {"tag": "CANARY-B4-VALUES"}},
        },
        "config": {"fromSet": "CANARY-B4-SET"},
        "manifest": "data:\n  manifest: CANARY-B4-MANIFEST\n",
    }

def helm_layer(raw: bytes) -> str:
    # Helm's own layer; kubectl adds the Kubernetes base64 on top.
    return base64.b64encode(gzip.compress(raw)).decode()

(work / "applied.release").write_text(helm_layer(json.dumps(release("applied")).encode()))
(work / "mismatch.release").write_text(helm_layer(json.dumps(release("other")).encode()))
(work / "bomb.release").write_text(helm_layer(b"\0" * (16 * 1024 * 1024)))
(work / "junk.release").write_text("!!not-a-helm-release!!")
applied = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {
        "name": "sh.helm.release.v1.applied.v1",
        "namespace": "b4-junk",
        "labels": {
            "owner": "helm",
            "name": "applied",
            "version": "1",
            "status": "deployed",
            "team": "CANARY-B4-LABEL",
        },
        "annotations": {"canary": "CANARY-B4-ANNOTATION"},
    },
    "type": "helm.sh/release.v1",
    "data": {
        "release": base64.b64encode((work / "applied.release").read_bytes()).decode()
    },
}
(work / "applied.json").write_text(json.dumps(applied))
PY
kubectl apply -f "$work/applied.json"
for name in junk mismatch bomb; do
    kubectl -n b4-junk create secret generic "sh.helm.release.v1.$name.v1" \
        --type=helm.sh/release.v1 --from-file=release="$work/$name.release"
    kubectl -n b4-junk label secret "sh.helm.release.v1.$name.v1" \
        owner=helm name="$name" version=1 status=deployed
done

# b4-a/plain-canary: an Opaque Secret no tool request may ever return.
kubectl -n b4-a create secret generic plain-canary --from-literal=value=CANARY-B4-OPAQUE

kubectl get secrets -A --field-selector type=helm.sh/release.v1 -o name
