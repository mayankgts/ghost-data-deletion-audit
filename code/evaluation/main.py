"""Score predictions against expected answers (sample_output.csv by default).

Usage: python3 code/evaluation/main.py [predictions.csv] [expected.csv] [-v]
"""
import csv
import json
import sys

KEY = ("phase", "operation", "system_id", "resource_id", "state")


def load(path):
    with open(path, encoding="utf-8") as f:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"] or "[]")) for r in csv.DictReader(f)}


def score(pred, gold, verbose=False):
    n = len(gold)
    status_ok = seq_ok = 0
    tp = fp = fn = 0
    ev_ok = ev_total = 0
    field_ok = {f: 0 for f in ("operation", "state", "evidence_id")}
    field_total = 0
    for cid, (gs, gsteps) in sorted(gold.items()):
        ps, psteps = pred.get(cid, ("MISSING", []))
        status_ok += ps == gs
        full = lambda steps: [tuple(s[k] for k in KEY) + (s["evidence_id"],) for s in steps]
        exact = full(psteps) == full(gsteps)
        seq_ok += exact
        gk = {tuple(s[k] for k in KEY) for s in gsteps}
        pk = {tuple(s[k] for k in KEY) for s in psteps}
        tp += len(gk & pk)
        fp += len(pk - gk)
        fn += len(gk - pk)
        gpos = {(s["phase"], s["system_id"], s["resource_id"]): s for s in gsteps}
        for s in psteps:
            g = gpos.get((s["phase"], s["system_id"], s["resource_id"]))
            if g:
                field_total += 1
                for f in field_ok:
                    field_ok[f] += s[f] == g[f]
        gev = {tuple(s[k] for k in KEY): s["evidence_id"] for s in gsteps}
        bad_ev = []
        for s in psteps:
            k = tuple(s[x] for x in KEY)
            if k in gev:
                ev_total += 1
                ev_ok += s["evidence_id"] == gev[k]
                if s["evidence_id"] != gev[k]:
                    bad_ev.append((k[1], k[3], s["evidence_id"], gev[k]))
        if verbose and not exact:
            print(f"{cid}: status {ps} (want {gs})")
            for k in sorted(gk - pk):
                print("   MISSING ", k[:4])
            for k in sorted(pk - gk):
                print("   EXTRA   ", k[:4])
            for b in bad_ev:
                print("   EVIDENCE", b[0], b[1], "got", repr(b[2]), "want", repr(b[3]))
            if gk == pk and not bad_ev:
                print("   ORDER differs")
    prec = tp / (tp + fp) if tp + fp else 0
    rec = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0
    return {"cases": n, "status_accuracy": status_ok / n, "step_precision": prec, "step_recall": rec,
            "step_f1": f1, "evidence_accuracy": ev_ok / ev_total if ev_total else 0,
            "exact_sequence_accuracy": seq_ok / n,
            **{f"field_{f}_accuracy": (v / field_total if field_total else 0) for f, v in field_ok.items()}}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "-v"]
    pred_path = args[0] if args else "code/cache/sample_predictions.csv"
    gold_path = args[1] if len(args) > 1 else "dataset/sample_output.csv"
    result = score(load(pred_path), load(gold_path), "-v" in sys.argv)
    for k, v in result.items():
        print(f"{k:24} {v:.3f}" if isinstance(v, float) else f"{k:24} {v}")