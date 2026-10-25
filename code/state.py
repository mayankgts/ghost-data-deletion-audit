"""Reconstruct, for one case, which resources belong to the user and what state each is in at audit_at."""
import csv
import json

CACHE = "code/cache/observations.json"

# field names that carry a direct identifier (name, email, phone, hash) in any evidence layout
DIRECT_FIELDS = ("email", "phone", "full_name", "name", "display_name", "email_hash", "requester_email",
                 "requester_name", "requester_phone", "owner_email", "owner_phone", "customer_email",
                 "document.email", "document.display_name", "profile.email", "profile.full_name",
                 "profile.phone", "profile.email_hash", "matched_value", "identifier")
REF_FIELDS = ("user_id", "customer_reference", "subject_reference", "subject_selector",
              "document.customer_reference", "profile.customer_reference")
TRUE = {"true", "1", "yes", "enabled", "active"}
FALSE = {"false", "0", "no", "disabled", "stopped", "blocked"}


def truthy(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return True if s in TRUE else False if s in FALSE else None


class Evidence:
    def __init__(self, path=CACHE):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.obs = d["observations"]
        # when each evidence file became available: nothing captured after audit_at may be used
        with open("dataset/evidence_files.csv", encoding="utf-8") as f:
            self.file_time = {r["file_id"]: r["captured_at"] for r in csv.DictReader(f)}
        self.by_rid = {}
        for o in self.obs:
            self.by_rid.setdefault(o["resource_id"], []).append(o)
        # a file with no records at all (failed/empty export) proves nothing
        filled = {o["evidence_id"] for o in self.obs if o["kind"] != "activity"}
        scopes = d.get("scopes", {})
        self.snapshots = []
        for s in d["snapshots"]:
            if s["evidence_id"] not in filled:
                continue
            if s.get("table"):
                # SQL dumps capture whole tables; an inventory only covers the systems in its scope
                sc = scopes.get(s["evidence_id"])
                s["complete"] = sc is None or s["system"] in sc
            else:
                # JSON bundles are filtered extracts: they only prove absence for users they cover
                s["complete"] = False
            self.snapshots.append(s)
        self.file_values = {}
        for o in self.obs:
            if o["kind"] != "activity":
                self.file_values.setdefault(o["evidence_id"], set()).update(o["values"])
        self.system_of = {}
        for o in self.obs:
            if o["system"] and o["kind"] != "activity":
                self.system_of.setdefault(o["resource_id"], o["system"])
        for o in self.obs:
            self.system_of.setdefault(o["resource_id"], o["system"])


def read_state(o):
    """One observation -> what it says about the resource at o['time']."""
    f, st = o["fields"], (o["state"] or "").upper()
    s = {"present": True, "identifiable": None, "direct": None, "flow_active": None, "restore_active": None,
         "ref_only": None}
    # ---- absence signals
    absent = (st in ("NOT_FOUND", "ABSENT")
              or str(f.get("status_code")) == "404" or str(f.get("http_status")) == "404"
              or str(f.get("rows")) == "0" or str(f.get("hits.total")) == "0"
              or ("profile" in f and f["profile"] is None))
    if absent:
        s["present"] = False
        return s
    # ---- identifiability
    direct = [k for k in DIRECT_FIELDS if f.get(k) not in (None, "", [], "null", "no direct identifier")]
    refs = [k for k in REF_FIELDS if f.get(k) not in (None, "", "null")]
    if st in ("IDENTIFIABLE", "FOUND", "PRESENT"):
        s["identifiable"] = True
    elif st in ("ANONYMIZED", "REDACTED"):
        s["identifiable"] = False
    elif o["kind"] == "log":
        s["identifiable"] = True if (direct or any(k in f for k in ("lookup_key", "match_key", "scan_key",
                                                                     "correlation_key", "subject_key"))) else None
    elif o["kind"] == "vision":
        s["identifiable"] = bool(f.get("identifiers"))
    else:
        s["identifiable"] = bool(direct or refs)
        # vision rows keep identifiers in a list; anything other than a bare user_id reference is direct
    vision_direct = o["kind"] == "vision" and any(not str(x).lower().startswith("user_") for x in f.get("identifiers") or [])
    s["direct"] = bool(direct) or vision_direct or st == "IDENTIFIABLE" or (o["kind"] == "log" and s["identifiable"] is True)
    # ---- data flows
    for k in ("enabled", "scheduler_registered", "flag_enabled"):
        if k in f and truthy(f[k]) is not None:
            s["flow_active"] = truthy(f[k])
            break
    if st in ("ACTIVE", "ENABLED"):
        s["flow_active"] = s["restore_active"] = True
    if st in ("DISABLED", "STOPPED"):
        s["flow_active"] = False
    # ---- backups
    if "restore_allowed" in f or "flag_restore_allowed" in f:
        s["restore_active"] = truthy(f.get("restore_allowed", f.get("flag_restore_allowed")))
    elif "eligible" in f or "deletion_block" in f:
        s["restore_active"] = truthy(f.get("eligible")) is not False and truthy(f.get("deletion_block")) is not True
    if st == "BLOCKED":
        s["restore_active"] = False
    return s


def state_at(ev, rid, audit_at, matcher=frozenset()):
    """State of rid at audit_at, plus the evidence to cite for it.

    State: explicit observations from files already captured at audit_at; only if there are none, record
    lines timestamped before audit_at in later exports. Absence inferred from a snapshot that omits the
    record is added on top. Activity rows are claims and are never used. The latest event wins, and at
    the same instant a surviving copy (replica, index) wins.
    Citation: the latest evidence that agrees with that state, preferring files captured by audit_at.
    Returns (state dict, evidence_id) or (None, None) when nothing is known."""
    system = ev.system_of.get(rid)
    explicit = []  # (time, state, evidence_id, admissible_at_audit)
    for o in ev.by_rid.get(rid, []):
        if o["kind"] == "activity" or o["time"] > audit_at:
            continue
        explicit.append((o["time"], read_state(o), o["evidence_id"], ev.file_time.get(o["evidence_id"], "") <= audit_at))
    events = [e for e in explicit if e[3]] or explicit
    for snap in ev.snapshots:
        covers_user = snap["complete"] or bool(matcher & ev.file_values.get(snap["evidence_id"], set()))
        if covers_user and snap["system"] == system and snap["time"] <= audit_at and rid not in snap["members"] \
                and ev.file_time.get(snap["evidence_id"], "") <= audit_at:
            events.append((snap["time"], {"present": False}, snap["evidence_id"], True))
    if not events:
        return None, None
    latest = max(e[0] for e in events)
    same = [e for e in events if e[0] == latest]
    present = [e for e in same if e[1]["present"]]
    pick = present or same
    merged = {"present": bool(present)}
    for key in ("identifiable", "direct", "flow_active", "restore_active", "ref_only"):
        vals = [e[1].get(key) for e in pick if e[1].get(key) is not None]
        merged[key] = any(vals) if vals else None
    agree = [e for e in events if e[1]["present"] == merged["present"]]
    cite = max([e for e in agree if e[3]] or agree, key=lambda e: (e[0], e[1]["present"]))
    return merged, cite[2]