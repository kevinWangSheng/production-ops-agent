"""List every claim/summary sentence of the base and metric_note samples that
mentions STATUS_CODE_UNSET together with a comparison/interpretation word, so a
reviewer can label them without knowing the variant.

usage: ECLASS_WORK=<dir> OUT=<dir> python unset_claims.py

Writes two files into OUT:

* unset-adjudication.json      shuffled sample ids (S001...) and claim texts
                                only; nothing that names the variant
* unset-adjudication-key.json  id -> sample name (which carries the variant)

Blind reading: the reviewer labels the first file, then opens the key.
Ids are assigned after a seeded shuffle of the sample names, so they neither
follow the variant nor the replay order and cannot be hashed back to a name.
"""

import glob
import json
import os
import random
import re

work = os.environ["ECLASS_WORK"]
pat = re.compile(
    r"dominat|non-error|not total|not all|exceed|larger|smaller|partial|majority|"
    r"most of|not a complete|not every|success|fraction|ratio|share",
    re.I,
)
by_sample: dict[str, list[dict]] = {}
for var in ("base", "metric_note"):
    for case in ("fault-1-at3", "fault-2"):
        for d in sorted(glob.glob(f"{work}/samples/{var}-{case}-r*")):
            meta = json.load(open(d + "/meta.json"))
            if meta["finish_reason"] != "stop":
                continue
            try:
                text = open(d + "/report.txt").read().strip()
                r = json.loads(re.sub(r"^```[a-z]*\s*|\s*```$", "", text))
            except ValueError:
                continue
            items = r["claims"] + [{"kind": "summary", "text": r.get("summary", "")}]
            rows = [
                {"claim_index": i, "kind": c["kind"], "text": c["text"]}
                for i, c in enumerate(items)
                if "UNSET" in c["text"] and pat.search(c["text"])
            ]
            by_sample[os.path.basename(d)] = rows
names = sorted(by_sample)
random.Random(20260929).shuffle(names)
key = {f"S{i + 1:03d}": n for i, n in enumerate(names)}
claims = [{"id": sid, **row} for sid, n in key.items() for row in by_sample[n]]
out = os.environ["OUT"]
json.dump({"claims": claims}, open(f"{out}/unset-adjudication.json", "w"), indent=1)
json.dump(key, open(f"{out}/unset-adjudication-key.json", "w"), indent=1)
