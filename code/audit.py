"""Audit one deletion request: link resources, read their state at audit_at, apply policy, emit steps."""
import re
import csv

from state import read_state, state_at

# ---------- policy ----------

def load_rules(path="dataset/deletion_rules.csv"):
    with open(path, encoding="utf-8") as f:
        return {r["data_category"]: r["required_action"] for r in csv.DictReader(f)}


PREFIX_CATEGORY = {"account": "USER_PROFILE", "search": "SEARCH_INDEX_ENTRY", "cache": "CACHE_ENTRY",
                   "analytics": "ANALYTICS_PROFILE", "telemetry": "PRODUCT_TELEMETRY", "export": "USER_EXPORT",
                   "marketing": "MARKETING_PROFILE", "email_delivery": "EMAIL_DELIVERY_LOG",
                   "audit_record": "SECURITY_AUDIT_LOG", "legal_hold": "LEGAL_HOLD_RECORD",
                   "backup": "BACKUP_ARCHIVE", "tombstone": "ACCOUNT_TOMBSTONE", "data_flow": "DATA_FLOW",
                   "support": "SUPPORT_MESSAGE", "object": "SUPPORT_ATTACHMENT", "identifier": "SECONDARY_IDENTIFIER"}
WORKER_ACTION = {"DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT", "ANONYMIZE_RECORD": "ANONYMIZE"}


def prefix(rid):
    return rid.rsplit("_", 1)[0]


def category_of(ev, rid, rules):
    """A category stated by structured evidence wins (a vision model's label is only a guess);
    otherwise infer from the record's shape and its prefix."""
    for o in ev.by_rid.get(rid, []):
        c = (o["category"] or "").upper()
        if o["kind"] not in ("activity", "vision") and c in rules:
            return c
    cat = PREFIX_CATEGORY.get(prefix(rid), "UNKNOWN")
    obs = [o for o in ev.by_rid.get(rid, []) if o["kind"] != "activity"]
    fields = [k for o in obs for k in o["fields"]]
    text = " ".join(str(v) for o in obs for v in o["values"])
    if cat == "SUPPORT_MESSAGE" and (any(k in fields for k in ("file_name", "file", "attachment", "uploaded_by"))
                                     or re.search(r"\.(pdf|jpe?g|png|docx?)\b|attachment", text)):
        cat = "SUPPORT_ATTACHMENT"
    if cat == "ANALYTICS_PROFILE" and "email_opt_in" in fields:
        cat = "MARKETING_PROFILE"
    return cat


def required_action(ev, rid, cat, rules):
    """Policy action; when the rule is conditional and evidence is silent, the deletion worker's own
    planned treatment for that record is used as a tie-breaker."""
    if cat in ("DATA_FLOW",):
        return "DISABLE"
    act = rules.get(cat)
    planned = [WORKER_ACTION[o["category"]] for o in ev.by_rid.get(rid, [])
               if o["kind"] == "activity" and o["category"] in WORKER_ACTION
               and o["fields"].get("source_system") == "deletion_worker"]
    if cat in ("SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT") and planned:
        return planned[-1]
    return act or (planned[-1] if planned else "DELETE")


# ---------- linking ----------

def link_resources(ev, matcher):
    """Every resource that any evidence (or activity text) ties to one of the user's identifiers."""
    linked = {}
    for o in ev.obs:
        # a log lookup that found nothing only shows what was searched for, not who owns the record
        if o["kind"] == "log" and not read_state(o)["present"]:
            continue
        if matcher & set(o["values"]):
            linked.setdefault(o["resource_id"], o["evidence_id"])
            for m in o.get("mentions", []):
                linked.setdefault(m, o["evidence_id"])
    # downstream copies: a sync/restore on a linked resource that repopulated another resource
    for o in ev.obs:
        if o["kind"] == "activity" and o["resource_id"] in linked:
            for m in o.get("mentions", []):
                linked.setdefault(m, o["evidence_id"])
    # records that point at a linked resource (e.g. a search document whose source is the account)
    for o in ev.obs:
        if o["kind"] != "activity" and any(str(v) in linked for k, v in o["fields"].items() if k.endswith("resource_id") and k != "resource_id"):
            linked.setdefault(o["resource_id"], o["evidence_id"])
    return linked


# ---------- steps ----------

FIND_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
REM_ORDER = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"]
VER_ORDER = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
             "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
STATE = {"REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
         "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
         "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
         "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
         "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
         "VERIFY_RESTORE_BLOCKED": "BLOCKED"}


def plan_resource(action, s):
    """(finding, remediation, verification) operations for one resource given its required action and state."""
    present, ident, direct = s["present"], s.get("identifiable"), s.get("direct")
    if action == "DISABLE":
        return ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED") if s.get("flow_active") \
            else (None, None, "VERIFY_DATA_FLOW_DISABLED")
    if action == "RETAIN" and s.get("restore_active") is not None:  # backups: retained but restore must be blocked
        return ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED") if s["restore_active"] \
            else (None, None, "VERIFY_RESTORE_BLOCKED")
    if action == "RETAIN":
        return (None, None, "VERIFY_RECORD_PRESENT") if present else (None, None, None)
    if not present:
        return (None, None, "VERIFY_RECORD_ABSENT")
    if action == "DELETE":
        return ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT")
    if action == "REDACT":
        return ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED") if direct \
            else (None, None, "VERIFY_RECORD_REDACTED")
    if action == "ANONYMIZE":
        return ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED") if ident \
            else (None, None, "VERIFY_RECORD_ANONYMIZED")
    return (None, None, None)


def audit_case(ev, rules, matcher, audit_at, extra_links=()):
    linked = link_resources(ev, matcher)
    for rid in extra_links:
        linked.setdefault(rid, "")
    items = []  # (phase, op, system, rid, evidence)
    for rid in linked:
        s, eid = state_at(ev, rid, audit_at, matcher)
        if s is None:
            continue
        cat = category_of(ev, rid, rules)
        f, r, v = plan_resource(required_action(ev, rid, cat, rules), s)
        system = ev.system_of.get(rid)
        if f:
            items.append(("FINDING", f, system, rid, eid))
        if r:
            items.append(("REMEDIATION", r, system, rid, ""))
        if v:
            items.append(("VERIFICATION", v, system, rid, eid))
        if not items:
        # last resort: nothing is linked to this person, but a lookup for their identifier came back empty
        # before the audit -> that lookup is the absence check (never used when anything else is linked)
            for o in ev.obs:
                if o["kind"] == "log" and o["time"] <= audit_at and ev.file_time.get(o["evidence_id"], "") <= audit_at \
                    and matcher & set(o["values"]) and not read_state(o)["present"]:
                    items.append(("VERIFICATION", "VERIFY_RECORD_ABSENT", ev.system_of.get(o["resource_id"]),
                              o["resource_id"], o["evidence_id"]))
        items = list(dict.fromkeys(items))
    complete = not any(i[0] == "FINDING" for i in items)
    if not complete:  # proposed future checks carry no evidence yet
        items = [(p, o, sy, r, e if p == "FINDING" else "") for p, o, sy, r, e in items]
    order = {op: i for i, op in enumerate(FIND_ORDER + REM_ORDER + VER_ORDER)}
    phase = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
    items.sort(key=lambda x: (phase[x[0]], order[x[1]], x[2] or "", x[3]))
    steps = [{"step_no": i + 1, "phase": p, "system_id": sy, "resource_id": r, "operation": o,
              "state": STATE[o], "evidence_id": e} for i, (p, o, sy, r, e) in enumerate(items)]
    return ("COMPLETE" if complete else "INCOMPLETE"), steps