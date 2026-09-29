"""List every claim/summary sentence of the base and metric_note samples that
mentions STATUS_CODE_UNSET together with a comparison/interpretation word, so a
reviewer can label them without knowing the variant.

usage: ECLASS_WORK=<dir> python unset_claims.py > unset-adjudication.json

Output rows carry a random-looking but stable id (sha1 of the sample name) and
the claim text; the variant key is kept in a separate 'key' map at the end.
"""

import glob
import hashlib
import json
import os
import re

work = os.environ["ECLASS_WORK"]
pat = re.compile(
    r"dominat|non-error|not total|not all|exceed|larger|smaller|partial|majority|"
    r"most of|not a complete|not every|success|fraction|ratio|share",
    re.I,
)
rows, key = [], {}
for var in ("base", "metric_note"):
    for case in ("fault-1-at3", "fault-2"):
        for d in sorted(glob.glob(f"{work}/samples/{var}-{case}-r*")):
            meta = json.load(open(d + "/meta.json"))
            if meta["finish_reason"] != "stop":
                continue
            try:
                r = json.loads(
                    re.sub(
                        r"^```[a-z]*\s*|\s*```$",
                        "",
                        open(d + "/report.txt").read().strip(),
                    )
                )
            except ValueError:
                continue
            name = os.path.basename(d)
            sid = hashlib.sha1(name.encode()).hexdigest()[:8]
            key[sid] = name
            for i, c in enumerate(
                r["claims"] + [{"kind": "summary", "text": r.get("summary", "")}]
            ):
                if "UNSET" in c["text"] and pat.search(c["text"]):
                    rows.append(
                        {
                            "id": sid,
                            "claim_index": i,
                            "kind": c["kind"],
                            "text": c["text"],
                        }
                    )
rows.sort(key=lambda x: (x["id"], x["claim_index"]))
print(json.dumps({"claims": rows, "key": key}, indent=1))
