import csv
import json
import os
import re
import sqlite3

DATA = "dataset"
CACHE = "code/cache"

TIME_KEYS = ("observed_at", "recorded_at", "captured_at", "exported_at")
SYSTEM_KEYS = ("data_store", "system")
RID = re.compile(r"\b(?:account|search|analytics|support|data_flow|backup|export|audit_record|"
                 r"tombstone|object|marketing|cache|email_delivery|telemetry|legal_hold|identifier)_\d+\b")


def manifest():
    with open(f"{DATA}/evidence_files.csv", encoding="utf-8") as f:
        return {r["file_path"]: r for r in csv.DictReader(f)}


def flatten(obj, prefix=""):
    """Nested dict -> {'a.b': value}. Lists of scalars are kept; lists of dicts are skipped."""
    out = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        elif isinstance(v, list):
            if all(not isinstance(x, (dict, list)) for x in v):
                out[key] = v
        else:
            out[key] = v
    return out


def values_of(obj):
    """Every scalar string anywhere inside obj, lowercased (used for identity matching)."""
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in values_of(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in values_of(v)]
    return [str(obj).strip().lower()] if obj not in (None, "") else []


def obs(file_id, time, system, rid, fields, kind, collection=None, category=None, state=None):
    return {"evidence_id": file_id, "time": time, "system": system, "resource_id": rid,
            "collection": collection, "category": category, "state": state, "kind": kind,
            "fields": fields, "values": sorted(set(values_of(fields)))}


# ---------- JSON: one generic walker for every JSON layout ----------

def parse_json(path, meta):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    observations, snapshots = [], []
    fid = meta["file_id"]

    def walk(node, ctx):
        if isinstance(node, list):
            for x in node:
                walk(x, ctx)
            return
        if not isinstance(node, dict):
            return
        ctx = dict(ctx)
        for k in TIME_KEYS:
            if isinstance(node.get(k), str):
                ctx["time"] = node[k]
                break
        for k in SYSTEM_KEYS:
            if isinstance(node.get(k), str):
                ctx["system"] = node[k]
        if isinstance(node.get("collection"), str):
            ctx["collection"] = node["collection"]
        # a list of records = full snapshot of that collection (used to prove ABSENT)
        if isinstance(node.get("records"), list):
            snapshots.append({"evidence_id": fid, "time": ctx["time"], "system": ctx.get("system"),
                              "collection": ctx.get("collection"),
                              "members": [r.get("resource_id") for r in node["records"] if isinstance(r, dict)]})
        if "resource_id" in node:
            observations.append(obs(fid, ctx["time"], ctx.get("system"), node["resource_id"],
                                    flatten(node), "json", ctx.get("collection"),
                                    node.get("record_type"), node.get("result")))
        for k, v in node.items():
            if k == "service_exports" and isinstance(v, dict):
                for svc, body in v.items():
                    for coll, items in body.items():
                        walk(items, {**ctx, "system": svc, "collection": coll})
            elif isinstance(v, (dict, list)):
                walk(v, ctx)

    walk(doc, {"time": meta["captured_at"]})
    return observations, snapshots


# ---------- TXT handover: "- time | system | resource | CATEGORY | STATE | identifier" ----------

def parse_txt(path, meta):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = [p.strip() for p in line.lstrip("- ").split("|")]
            if len(parts) == 6 and RID.fullmatch(parts[2]):
                t, system, rid, cat, state, ident = parts
                out.append(obs(meta["file_id"], t, system, rid, {"identifier": ident}, "txt",
                               category=cat, state=state))
    return out


# ---------- system_activity.csv: claims, but details also carry identity links ----------

def parse_activity():
    out = []
    with open(f"{DATA}/system_activity.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            o = obs(r["activity_id"], r["event_time"], r["target_system"], r["resource_id"],
                    {"details": r["details"], "source_system": r["source_system"]}, "activity",
                    category=r["event_type"], state=r["reported_result"])
            o["values"] = sorted(set(re.findall(r"[\w.+-]+@[\w.-]+|\buser_\d+\b", r["details"].lower())))
            o["mentions"] = RID.findall(r["details"])  # e.g. "repopulated downstream resource search_4801"
            out.append(o)
    return out

# ---------- SQL snapshots: load into in-memory SQLite and read every table ----------

def parse_sql(path, meta):
    db = sqlite3.connect(":memory:")
    with open(path, encoding="utf-8") as f:
        db.executescript(f.read())
    db.row_factory = sqlite3.Row
    fid, captured = meta["file_id"], meta["captured_at"]
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    # inventory files declare which systems were fully captured (and when)
    scope = {}
    if "snapshot_scope" in tables:
        scope = {r["origin_system"]: r["captured_at"] for r in db.execute("SELECT * FROM snapshot_scope")}
    observations, groups = [], {}
    for t in tables:
        if t == "snapshot_scope":
            continue
        for row in db.execute(f'SELECT * FROM "{t}"'):
            row = dict(row)
            if "record_json" in row:  # replica layout: one JSON record per row
                fields = json.loads(row["record_json"])
                system, coll = row["origin_system"], row["collection_name"]
            else:
                fields, system, coll = row, None, t
            fields["_table"] = t
            observations.append(obs(fid, captured, system, row["resource_id"], fields, "sql", coll))
            groups.setdefault((t, system, coll), []).append(row["resource_id"])
        if not any(k[0] == t for k in groups):  # empty table = still a snapshot of "nothing there"
            groups[(t, None, t)] = []
    snapshots = [{"evidence_id": fid, "time": captured, "system": sys_, "collection": coll,
                  "table": t, "members": m} for (t, sys_, coll), m in groups.items()]
    return observations, snapshots, scope


# ---------- Logs: normalise every format, then join lines that share a trace ----------

KV = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def kv_pairs(text):
    out = {}
    for k, v in KV.findall(text):
        if v.startswith('"'):
            v = v[1:-1].replace('\\"', '"')
        out[k] = v
        if k == "message" and "=" in v:  # cloud runtime nests key=value inside message="..."
            out.update(kv_pairs(v))
    return out


def tokenize(line):
    """Any supported log line -> dict with time, service, trace, event and all key/values."""
    m = re.match(r"(\d{4}-\d\d-\d\dT[\d:]+Z)\s+(.*)", line.strip())
    if not m:
        return None
    t, rest = m.groups()
    d = {}
    if rest.startswith("{"):                                   # JSON lines
        d = json.loads(rest)
    elif rest.startswith("["):                                 # [LEVEL] [service] [trace] event: k="v"
        b = re.match(r"\[(\w+)\] \[([\w-]+)\] \[([\w-]+)\] (\w+):(.*)", rest)
        if b:
            d = {"level": b[1], "service": b[2], "trace_id": b[3], "event": b[4], **kv_pairs(b[5])}
    elif " | " in rest:                                        # LEVEL | service | trace | event | k="v"
        p = [x.strip() for x in rest.split(" | ")]
        d = {"level": p[0], "service": p[1], "trace_id": p[2], "event": p[3], **kv_pairs(" ".join(p[4:]))}
    elif rest.startswith("<"):                                 # syslog: <15>1 node service trace - - k="v"
        p = rest.split(" ", 5)
        d = {"service": p[2], "trace_id": p[3], **kv_pairs(p[5] if len(p) > 5 else "")}
    else:                                                      # logfmt (with or without level=)
        d = kv_pairs(rest)
        if "function" in d:
            d["service"] = d["function"]
        if "requestId" in d:
            d["trace_id"] = d["requestId"]
    d["time"] = t
    return d


def parse_log(path, meta):
    lines = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            d = tokenize(raw)
            if d:
                lines.append(d)
    traces = {}
    for d in lines:
        if d.get("trace_id"):
            traces.setdefault((d.get("service"), d["trace_id"]), []).append(d)
    out = []
    for d in lines:
        if not d.get("resource_id"):
            continue
        merged = {}
        for other in traces.get((d.get("service"), d.get("trace_id")), [d]):
            merged.update({k: v for k, v in other.items() if k != "time"})
        merged.update(d)  # the resource line wins on conflicts
        merged["log_file"] = path.rsplit("/", 1)[-1]
        out.append(obs(meta["file_id"], d["time"], d.get("system") or d.get("service"), d["resource_id"],
                       merged, "log", category=d.get("record_type") or d.get("event"),
                       state=d.get("result")))
    return out

# ---------- Images / PDFs: read the cached vision-model output (built by code/vision.py) ----------

VISION_CACHE = "code/cache/vision"


def parse_vision(meta):
    path = f"{VISION_CACHE}/{meta['file_id']}.json"
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        v = json.load(f)
    subj = v.get("subject") or {}
    subject_ids = [x for k in ("user_ids", "emails", "phones") for x in subj.get(k) or []]
    out = []
    for r in v.get("records") or []:
        rid = (r.get("resource_id") or "").strip()
        if not RID.fullmatch(rid):
            continue
        idents = list(r.get("identifiers") or [])
        if rid in (subj.get("resource_ids") or []):  # resource shown in the page header belongs to the subject
            idents += subject_ids
        fields = {"identifiers": idents, "status": r.get("status"), "notes": r.get("notes"),
                  "screen_title": v.get("screen_title"), "record_time": r.get("time"),
                  **{f"flag_{k}": x for k, x in (r.get("flags") or {}).items() if x is not None}}
        if meta["file_path"].endswith(".pdf"):  # the evidence is the document itself (e.g. an uploaded file)
            fields["source_document"] = meta["file_path"].rsplit("/", 1)[-1]
        # a date printed on a record is when it was created; the evidence was observed at capture time
        out.append(obs(meta["file_id"], meta["captured_at"], r.get("system_hint"), rid, fields, "vision",
                       category=r.get("record_type"), state=r.get("status")))
    return out


def prefix(rid):
    return rid.rsplit("_", 1)[0] if rid else ""


def fill_missing_systems(observations, snapshots):
    """Learn resource-prefix -> owning system from the data itself, then fill gaps (no hardcoding).
    Votes come from evidence records that name their store, plus deletion-worker actions
    (those always target the owning system)."""
    votes = {}
    for o in observations:
        owner = o["fields"].get("source_system") == "deletion_worker" if o["kind"] == "activity" else bool(o["system"])
        if owner:
            v = votes.setdefault(prefix(o["resource_id"]), {})
            v[o["system"]] = v.get(o["system"], 0) + 1
    best = {p: max(v, key=v.get) for p, v in votes.items()}
    owners = set(best.values())
    for o in observations:
        if o["system"] not in owners:
            o["system"] = best.get(prefix(o["resource_id"]), o["system"])
    for s in snapshots:
        if s["system"] not in owners and s["members"]:
            s["system"] = best.get(prefix(s["members"][0]), s["system"])
    # an empty SQL table still proves absence: learn its system from same-named tables elsewhere
    by_table = {s["table"]: s["system"] for s in snapshots if s.get("table") and s["members"] and s["system"]}
    for s in snapshots:
        if not s["system"] and s.get("table"):
            s["system"] = by_table.get(s["table"])


def build():
    observations, snapshots, scopes = [], [], {}
    for path, meta in manifest().items():
        full = f"{DATA}/{path}"
        if path.endswith(".json"):
            o, s = parse_json(full, meta)
            observations += o
            snapshots += s
        elif path.endswith(".txt"):
            observations += parse_txt(full, meta)
        elif path.endswith(".sql"):
            o, s, scope = parse_sql(full, meta)
            observations += o
            snapshots += s
            if scope:
                scopes[meta["file_id"]] = scope
        elif path.endswith(".log"):
            observations += parse_log(full, meta)
        elif path.endswith(".log"):
            observations += parse_log(full, meta)
        elif path.endswith((".png", ".pdf")):
            observations += parse_vision(meta)
    observations += parse_activity()
    fill_missing_systems(observations, snapshots)
    os.makedirs(CACHE, exist_ok=True)
    with open(f"{CACHE}/observations.json", "w", encoding="utf-8") as f:
        json.dump({"observations": observations, "snapshots": snapshots, "scopes": scopes}, f)
    return observations, snapshots


if __name__ == "__main__":
    o, s = build()
    kinds = {}
    for x in o:
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    print("observations:", len(o), kinds, "| collection snapshots:", len(s))