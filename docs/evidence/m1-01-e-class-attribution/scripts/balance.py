"""Query the DeepSeek balance; key is read from M0_ENV_FILE and never printed.

usage: balance.py <out.json> <note>
"""

import json
import os
import sys
import time
import urllib.request

path = os.environ["M0_ENV_FILE"]
key = ""
with open(path) as f:
    for line in f:
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
assert key, "DEEPSEEK_API_KEY missing"
req = urllib.request.Request(
    "https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"}
)
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
    req, timeout=30
) as r:
    data = json.loads(r.read())
data["queried_at_utc"] = (
    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + f" ({sys.argv[2]})"
)
with open(sys.argv[1], "w") as f:
    json.dump(data, f, indent=0)
    f.write("\n")
print(json.dumps(data))
