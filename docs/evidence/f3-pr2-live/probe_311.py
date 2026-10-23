"""#311 lab probe: one deliberately broken PromQL through the real product
executor and the kind lab read-only proxy; compares the raw 400 body via the
proxy with the body from Prometheus itself (port-forward). No model call.
Credentials are read from the lab env files and never printed."""
import base64, hashlib, json, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from opspilot.tools.otel_demo import (METRICS_TOOL, TARGET_ID, OtelDemoConfig,
    otel_demo_executor_factory, otel_demo_face, scope_window)
from tests.m1_tool_support import RecordingSink
from tests.test_m1_otel_demo_contract import FakeStore, _call, _lease

LAB = "/Users/shenghuikevin/dev/AI/production-ops-agent/tmp/m1-kind-lab/"
def envfile(name):
    out = {}
    for line in open(LAB + name):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); out[k.replace("export ", "").strip()] = v.strip().strip("'\"")
    return out

inv = envfile("prometheus-investigator.env")
lab = envfile("prometheus-lab.env")
PROXY = "http://127.0.0.1:19090"
DIRECT = sys.argv[1]  # port-forwarded Prometheus service
EXPR = 'sum(rate(traces_span_metrics_calls_total{service_name="checkout"}[5m])'

class Clock:
    def now(self): return datetime.now(timezone.utc)
    def monotonic(self): return time.monotonic()

clock = Clock()
face = otel_demo_face(clock)
lease = _lease()
store = FakeStore(lease, deadline=clock.now() + timedelta(minutes=10))
input = face.input_for(run_id=str(lease.run_id), question="probe", target_id=TARGET_ID,
                       deadline=clock.now() + timedelta(minutes=10), model_requests=2)
config = OtelDemoConfig(prometheus_url=PROXY,
    prometheus_username=inv["OPSPILOT_OTEL_PROMETHEUS_USERNAME"],
    prometheus_password=inv["OPSPILOT_OTEL_PROMETHEUS_PASSWORD"])
factory = otel_demo_executor_factory(store, evidence=RecordingSink(), clock=clock, config=config)
executor = factory(lease, input)
window = scope_window(input)
outcome = executor.execute(_call(METRICS_TOOL, {"expr": EXPR}, window=window))

def raw(base, user, pw):
    end = clock.now(); start = end - timedelta(minutes=5)
    url = base + "/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": EXPR, "start": f"{start.timestamp():.3f}", "end": f"{end.timestamp():.3f}", "step": "30"})
    h = {"Accept": "application/json",
         "Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        r = opener.open(urllib.request.Request(url, headers=h), timeout=10); return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)

ps, pb, ph = raw(PROXY, inv["OPSPILOT_OTEL_PROMETHEUS_USERNAME"], inv["OPSPILOT_OTEL_PROMETHEUS_PASSWORD"])
# Prometheus itself accepts only the in-cluster upstream accounts since F7;
# the proxy account is what nginx forwards with.
ds, db, dh = raw(DIRECT, "proxy", json.load(open(LAB + "prometheus-auth.json"))["proxy"])
print(json.dumps({
    "at": clock.now().isoformat(),
    "expr": EXPR,
    "executor": {"status": outcome.status, "reason": outcome.reason,
                 "source_contact": outcome.source_contact,
                 "evidence_is_none": outcome.evidence is None,
                 "model_view": outcome.model_view},
    "raw_via_proxy": {"http_status": ps, "content_type": ph.get("Content-Type"),
                      "body": pb.decode("utf-8", "replace"), "sha256": hashlib.sha256(pb).hexdigest(), "bytes": len(pb)},
    "raw_direct_prometheus": {"http_status": ds, "content_type": dh.get("Content-Type"),
                      "body": db.decode("utf-8", "replace"), "sha256": hashlib.sha256(db).hexdigest(), "bytes": len(db)},
    "bodies_identical": pb == db,
}, indent=2, default=str))
