import csv
import hashlib

DATA = "dataset"


def sha(email):
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def load_csv(name):
    with open(f"{DATA}/{name}", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class IdentityResolver:
    def __init__(self):
        self.users = {r["user_id"]: r for r in load_csv("user_identities.csv")}
        self.audit = load_csv("user_audit_log.csv")
        self.by_value = {}      
        for uid, r in self.users.items():
            for v in (r["email"], r["phone"], r["email_hash"]):
                self.by_value[v.strip().lower()] = uid
        for a in self.audit:
            for v in (a["old_value"], a["new_value"]):
                self.by_value.setdefault(v.strip().lower(), a["user_id"])

    def resolve(self, email):
        """Return (user_id, identifiers dict) for a request email, or (None, None)."""
        uid = self.by_value.get(email.strip().lower())
        if not uid:
            return None, None
        u = self.users[uid]
        emails, phones, hashes = {u["email"]}, {u["phone"]}, {u["email_hash"]}
        audit_ids = []
        for a in self.audit:
            if a["user_id"] != uid:
                continue
            audit_ids.append(a["audit_id"])
            bucket = {"email": emails, "phone": phones, "email_hash": hashes}[a["field_name"]]
            bucket.update((a["old_value"], a["new_value"]))
        hashes |= {sha(e) for e in emails}
        ids = {
            "user_id": uid,
            "emails": sorted(e.lower() for e in emails if e),
            "phones": sorted(p for p in phones if p),
            "hashes": sorted(h.lower() for h in hashes if h),
            "audit_ids": audit_ids,
        }
        return uid, ids

    def matcher(self, ids):
        """Set of lowercase values that mark a record as belonging to this user."""
        return {ids["user_id"].lower(), *ids["emails"], *ids["hashes"],
                *(p.lower() for p in ids["phones"])}


if __name__ == "__main__":
    r = IdentityResolver()
    reqs = load_csv("deletion_requests.csv") + load_csv("sample_deletion_requests.csv")
    missing = [q["case_id"] for q in reqs if r.resolve(q["email"])[0] is None]
    print("requests:", len(reqs), "unresolved:", len(missing), missing[:10])
    print(r.resolve("novak.tomasz@icloud.com"))