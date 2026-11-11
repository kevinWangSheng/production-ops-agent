#!/bin/bash
# flag.sh: read-only, print the paymentFailure flag in the lab flagd ConfigMap
cd ~/production-ops-agent
kubectl --kubeconfig tmp/m1-kind-lab/kubeconfig --context kind-opspilot-m1 -n otel-demo get configmap flagd-config -o json \
 | python3 -c 'import sys,json; d=json.load(sys.stdin)["data"]
for k,v in d.items():
    f=json.loads(v)["flags"].get("paymentFailure"); print("flagd", k, "paymentFailure defaultVariant=", f and f.get("defaultVariant"))'
