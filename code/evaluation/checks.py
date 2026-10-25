"""Evaluation part 2: contract checks on every output row + custom evaluation cases.

Usage: python3 code/evaluation/checks.py [output.csv]

1. Contract checks (no labels needed, run on all 518 rows): schema, allowed phase/operation/state
   combinations, step numbering, required ordering, evidence rules, and "no evidence from the future".
2. Custom cases: requests we wrote ourselves, each targeting one failure mode, with expectations derived
   from the problem statement or from a labelled sample (not from our own output).
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import run  # noqa: E402

OPS = {  # operation -> (phase, state), from the problem statement
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"), "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"), "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"), "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"), "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"), "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"), "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"), "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}
ORDER = {op: i for i, op in enumerate(OPS)}
PHASE = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}


def read(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def contract_errors(row, request, file_time):
    errs = []
    status, steps = row["deletion_status"], json.loads(row["steps_json"])
    if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
        errs.append(f"bad status {status}")
    if status == "NOT_APPLICABLE" and steps:
        errs.append("NOT_APPLICABLE must have []")
    if status != "NOT_APPLICABLE" and not steps:
        errs.append("audited case with no steps")
    if [s["step_no"] for s in steps] != list(range(1, len(steps) + 1)):
        errs.append("step_no not 1..n")
    keys = [(PHASE[s["phase"]], ORDER[s["operation"]], s["system_id"], s["resource_id"]) for s in steps]
    if keys != sorted(keys):
        errs.append("steps not in required order")
    for s in steps:
        if OPS.get(s["operation"]) != (s["phase"], s["state"]):
            errs.append(f"{s['operation']} with phase/state {s['phase']}/{s['state']}")
        ev = s["evidence_id"]
        if status == "COMPLETE" and s["phase"] != "VERIFICATION":
            errs.append("COMPLETE case contains a non-verification step")
        cited = s["phase"] == "FINDING" or status == "COMPLETE"
        if cited and not ev:
            errs.append(f"{s['operation']} {s['resource_id']} must cite evidence")
        if not cited and ev:
            errs.append(f"proposed step {s['operation']} must not cite evidence")
        if ev.startswith("evidence_") and file_time.get(ev, "") > request["audit_at"]:
            errs.append(f"cites {ev} captured after audit_at")
    if status == "INCOMPLETE" and not any(s["phase"] == "FINDING" for s in steps):
        errs.append("INCOMPLETE without findings")
    return errs


CUSTOM = [
    # (case, why, check) -- each check returns None when it passes, else a message
    ({"case_id": "custom_01", "email": "s.yoon04@gmail.com", "requested_at": "2026-08-31T12:00:00Z",
      "reported_status": "NOT_STARTED", "reported_at": "2026-09-02T12:00:00Z", "audit_at": "2026-09-15T12:00:00Z",
      "request_text": "Please stop the newsletters, but keep my account and my order history as they are."},
     "real customer (same user as sample case_25) who is NOT asking for deletion -> NOT_APPLICABLE, []",
     lambda st, steps, gold: None if (st, steps) == ("NOT_APPLICABLE", []) else f"got {st} with {len(steps)} steps"),
    ({"case_id": "custom_02", "email": "MINJUN.KIM@GMAIL.COM", "requested_at": "2026-08-24T12:00:00Z",
      "reported_status": "COMPLETE", "reported_at": "2026-08-25T12:00:00Z", "audit_at": "2026-09-15T12:00:00Z",
      "request_text": "Bitte löschen Sie mein Konto und alle zugehörigen personenbezogenen Daten."},
     "sample case_12 re-sent in upper case and in German -> must equal the labelled case_12 answer",
     lambda st, steps, gold: None if (st, steps) == gold["case_12"] else "differs from labelled case_12"),
    ({"case_id": "custom_03", "email": "g.silva76@gmail.com", "requested_at": "2026-08-24T12:00:00Z",
      "reported_status": "COMPLETE", "reported_at": "2026-08-25T12:00:00Z", "audit_at": "2026-09-15T12:00:00Z",
      "request_text": "Please delete my account."},
     "look-alike decoy: search_1874 belongs to g.silva76.jr@gmail.com and must never appear",
     lambda st, steps, gold: None if all(s["resource_id"] != "search_1874" for s in steps) else "decoy linked"),
    ({"case_id": "custom_04", "email": "keiko.nakamura@gmail.com", "requested_at": "2026-09-01T12:00:00Z",
      "reported_status": "COMPLETE", "reported_at": "2026-09-02T12:00:00Z", "audit_at": "2026-08-01T12:00:00Z",
      "request_text": "Please erase my account."},
     "time travel: sample case_24 audited a month earlier -> nothing captured after audit_at may be cited",
     lambda st, steps, gold: None if all(s["evidence_id"] in ("", "evidence_") or s["evidence_id"].startswith("activity_")
                                          or FILE_TIME.get(s["evidence_id"], "") <= "2026-08-01T12:00:00Z" for s in steps)
     else "cited evidence from after audit_at"),
    ({"case_id": "custom_05", "email": "leonie.g9352@outlook.com", "requested_at": "2026-08-19T12:00:00Z",
      "reported_status": "NOT_STARTED", "reported_at": "2026-08-22T12:00:00Z", "audit_at": "2026-09-15T12:00:00Z",
      "request_text": "I want to close this account permanently."},
     "failed lookup is not ownership: account_6523 was only searched for with this email (404) -> must not appear",
     lambda st, steps, gold: None if all(s["resource_id"] != "account_6523" for s in steps) else "decoy account linked"),
]


def load_gold():
    return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in read("dataset/sample_output.csv")}


with open("dataset/evidence_files.csv", encoding="utf-8") as _f:
    FILE_TIME = {r["file_id"]: r["captured_at"] for r in csv.DictReader(_f)}


def main(out_path="output.csv"):
    requests = {r["case_id"]: r for r in read("dataset/deletion_requests.csv")}
    rows = read(out_path)
    bad = {}
    for r in rows:
        e = contract_errors(r, requests[r["case_id"]], FILE_TIME)
        if e:
            bad[r["case_id"]] = e
    print(f"contract checks: {len(rows) - len(bad)}/{len(rows)} rows pass")
    for cid, e in list(bad.items())[:10]:
        print(f"   {cid}: {'; '.join(sorted(set(e)))}")

    # custom cases: write them as a request file and run them through the real pipeline
    tmp_in, tmp_out = "code/cache/custom_requests.csv", "code/cache/custom_predictions.csv"
    with open(tmp_in, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(CUSTOM[0][0]))
        w.writeheader()
        for case, _, _ in CUSTOM:
            w.writerow(case)
    run(tmp_in, tmp_out)
    preds = {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in read(tmp_out)}
    gold, passed = load_gold(), 0
    for case, why, check in CUSTOM:
        st, steps = preds[case["case_id"]]
        msg = check(st, steps, gold)
        passed += msg is None
        print(f"{case['case_id']}: {'PASS' if msg is None else 'FAIL - ' + msg}  | {why}")
    print(f"custom cases: {passed}/{len(CUSTOM)} pass")


if __name__ == "__main__":
    main(*sys.argv[1:])