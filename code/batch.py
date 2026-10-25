"""Reconstruct which deletion-worker actions belong to which request when no evidence names the user.

The deletion worker processes every request reported on a given day as one batch, and its result for a
request's primary profile mirrors the status the workflow later reported:
    COMPLETE -> SUCCESS, PARTIALLY_COMPLETE -> PARTIAL, NOT_STARTED -> FAILED
So an unlinked primary-profile deletion is assigned to a request only when day + status class +
elimination leave exactly one candidate. Ambiguous slots stay unassigned (a wrong guess costs more than a
missing step). Other categories are not reconstructed: a request may legitimately have no search entry or
analytics profile, so "this request lacks one" is not evidence (tried: 0 correct, 1 wrong on the samples).
"""
from audit import link_resources, prefix
from state import read_state

STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}


def minutes(t_from, t_to):
    """Minutes from one ISO-8601 UTC time to another (same day, hh:mm:ss precision is enough)."""
    def m(t):
        hh, mm, ss = t[11:19].split(":")
        return int(t[8:10]) * 1440 + int(hh) * 60 + int(mm) + int(ss) / 60
    return m(t_to) - m(t_from)


def assign(ev, cases, matchers):
    """cases: list of request dicts (all known requests, used as context); matchers: case_id -> identifier set.
    Returns case_id -> set of extra resource ids."""
    links = {c["case_id"]: set(link_resources(ev, matchers[c["case_id"]])) for c in cases if c["case_id"] in matchers}
    owned = set().union(*links.values()) if links else set()
    # weak evidence: the identifier a failed lookup was searching for
    searched = {}
    for o in ev.obs:
        if o["kind"] == "log" and not read_state(o)["present"]:
            searched.setdefault(o["resource_id"], set()).update(o["values"])
    worker = [o for o in ev.obs if o["kind"] == "activity" and o["fields"].get("source_system") == "deletion_worker"
              and o["category"] == "DELETE_RECORD" and prefix(o["resource_id"]) == "account"
              and o["resource_id"] not in owned]
    
    extra = {cid: set() for cid in links}
    # a person who re-sends the same request on the same day is one deletion job, not two competitors
    members, by_case = {}, {}
    for c in cases:
        if c["case_id"] not in links:
            continue
        job = (frozenset(matchers[c["case_id"]]), c["reported_at"][:10], c["reported_status"])
        rep = members.setdefault(job, [c["case_id"]])[0]
        if rep == c["case_id"]:
            by_case[rep] = c
        else:
            members[job].append(c["case_id"])

    def has(cid, p):
        return any(prefix(r) == p for r in links[cid] | extra[cid])

    for p in sorted({prefix(o["resource_id"]) for o in worker}):
        pool = [o for o in worker if prefix(o["resource_id"]) == p]
        days = sorted({o["time"][:10] for o in pool})
        for day in days:
            items = [o for o in pool if o["time"][:10] == day]
            need = [cid for cid, c in by_case.items() if c["reported_at"][:10] == day and not has(cid, p)]
            changed = True
            while changed and items and need:
                changed = False
                for status, result in STATUS_RESULT.items():
                    ci = [cid for cid in need if by_case[cid]["reported_status"] == status]
                    it = [o for o in items if o["state"] == result]
                    pick = None
                    if len(ci) == 1 and len(it) == 1:
                        pick = (ci[0], it[0])
                    else:
                        # tie-break 1: a failed lookup that searched for exactly one candidate's identifier
                        for o in it:
                            hits = [cid for cid in ci if matchers[cid] & searched.get(o["resource_id"], set())]
                            if len(hits) == 1:
                                pick = (hits[0], o)
                                break
                        # tie-break 2: the worker finished minutes before the workflow reported this request
                        if not pick:
                            for cid in ci:
                                near = [o for o in it if 0 <= minutes(o["time"], by_case[cid]["reported_at"]) <= 5]
                                rivals = [c2 for c2 in ci if near and 0 <= minutes(near[0]["time"], by_case[c2]["reported_at"]) <= 5]
                                if len(near) == 1 and rivals == [cid]:
                                    pick = (cid, near[0])
                                    break
                    if pick:
                        cid, o = pick
                        extra[cid].add(o["resource_id"])
                        need.remove(cid)
                        items.remove(o)
                        changed = True
                        break
    for group in members.values():
        for cid in group[1:]:
            extra[cid] |= extra[group[0]]
    return extra