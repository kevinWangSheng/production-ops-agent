#!/usr/bin/env bash
# C4 (#568) CI seed: the synthetic ArgoCD Applications, an ApplicationSet and
# an AppProject (apps.yaml), written with the runner's admin kubeconfig. The
# Application CRD has no status subresource, so status is written with the
# object; no ArgoCD controller runs. __RECENT__ becomes one hour ago (inside
# the authorized window), the fixed dates stay outside it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
recent=$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)
rendered=$(mktemp)
sed "s/__RECENT__/$recent/g" "$here/apps.yaml" > "$rendered"
kubectl create -f "$rendered" 2>/dev/null || kubectl replace -f "$rendered"
rm -f "$rendered"
kubectl get applications -A -o name
