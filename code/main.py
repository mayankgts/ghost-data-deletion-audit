"""Run the deletion audit agent over a requests CSV and write predictions.

Usage:
    python3 code/main.py                 # dataset/deletion_requests.csv -> output.csv
    python3 code/main.py --samples       # dataset/sample_deletion_requests.csv -> code/cache/sample_predictions.csv
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from audit import audit_case, load_rules  # noqa: E402
from batch import assign  # noqa: E402
from identity import IdentityResolver  # noqa: E402
from state import Evidence  # noqa: E402
from identity import IdentityResolver  # noqa: E402
from intent import classify, is_deletion  # noqa: E402


ALL_REQUESTS = ("dataset/deletion_requests.csv", "dataset/sample_deletion_requests.csv")


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run(requests_path, out_path):
    cases = read_csv(requests_path)
    ev, rules, idr = Evidence(), load_rules(), IdentityResolver()
    # every known request is context for reconstructing the deletion worker's batches
    context = [c for p in ALL_REQUESTS for c in read_csv(p)]
    known = {c["case_id"] for c in context}
    context += [c for c in cases if c["case_id"] not in known]
    intents = classify(c["request_text"] for c in context)
    # only authorised deletions are audited (and only they took part in the deletion worker's batches)
    context = [c for c in context if is_deletion(intents, c["request_text"])]
    matchers = {}
    for c in context:
        uid, ids = idr.resolve(c["email"])
        if uid:
            matchers[c["case_id"]] = idr.matcher(ids)
    extra = assign(ev, context, matchers)
    rows = []
    for c in cases:
        if c["case_id"] not in matchers:  # not a deletion request, or no such user
            rows.append((c["case_id"], "NOT_APPLICABLE", []))
            continue
        status, steps = audit_case(ev, rules, matchers[c["case_id"]], c["audit_at"], extra.get(c["case_id"], ()))
        rows.append((c["case_id"], status, steps))
    rows.sort(key=lambda r: int(r[0].split("_")[1]))  # numeric: case_33 ... case_550
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for cid, status, steps in rows:
            w.writerow([cid, status, json.dumps(steps, separators=(",", ":"))])
    print(f"wrote {len(rows)} rows -> {out_path}")
    assert len(rows) == len(cases), f"expected {len(cases)} rows, wrote {len(rows)}"
    assert len({r[0] for r in rows}) == len(rows), "duplicate case_id in output"


if __name__ == "__main__":
    if "--samples" in sys.argv:
        run("dataset/sample_deletion_requests.csv", "code/cache/sample_predictions.csv")
    elif len(sys.argv) == 3:
        run(sys.argv[1], sys.argv[2])
    else:
        run("dataset/deletion_requests.csv", "output.csv")