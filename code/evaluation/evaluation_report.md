# Evaluation report

## Approach

The agent reconstructs evidence instead of generating answers. Every evidence file (JSON bundles, SQLite
snapshot dumps, seven log formats, a handover text file, screenshots and PDFs) is normalised into one table
of observations: at time T, system S held resource R in state X, tied to identifier I. Identity resolution
expands the request email into every current and historical identifier: old emails, phones, SHA-256 email
hashes and the stable user_id. Resources are linked to the user through those identifiers, and their state
at `audit_at` is rebuilt from evidence that existed at that time. `deletion_rules.csv` then decides the
required treatment, and the steps are emitted in the required order. LLMs do two jobs only: reading the 15
images and PDFs once, and deciding whether each request asks for deletion at all.

## Results on the labelled samples (32 cases)

Command: `python code/main.py --samples && python code/evaluation/main.py`

| Metric | Score |
|---|---|
| Case-level status accuracy | 100.0% (32/32) |
| Step-level precision | 100.0% (every emitted step is in the answer) |
| Step-level recall | 90.7% |
| Step-level F1 | 95.1% |
| Field accuracy: operation | 100.0% |
| Field accuracy: state | 100.0% |
| Field accuracy: evidence_id | 100.0% |
| Exact sequence accuracy (whole steps_json identical) | 50.0% (16/32) |

Steps are compared on (phase, operation, system_id, resource_id, state). Field accuracy aligns predicted and
expected steps on (phase, system_id, resource_id). The code builds the order directly from the rules, and
the contract checks below confirm it, so each of the 16 sequence misses comes from a missing step. None of
them comes from steps in the wrong order.

## Results on the full dataset (518 cases, unlabelled)

Command: `python code/main.py && python code/evaluation/checks.py`

- 518 rows, numerically sorted, one per request: 361 INCOMPLETE, 147 COMPLETE, 10 NOT_APPLICABLE.
- Contract checks: 518/518 rows pass. Each row is checked for allowed status values; `[]` exactly for
  NOT_APPLICABLE; step_no running 1..n; each operation's phase and state matching the problem statement's
  table; ordering by phase, then operation order, then system_id, then resource_id; COMPLETE rows holding
  only evidence-backed verification steps; findings citing evidence while proposed remediation and
  verification steps cite none; INCOMPLETE rows containing findings; and no cited evidence file captured
  after the case's `audit_at`.

## Intent (NOT_APPLICABLE) evaluation

The samples contain no non-deletion requests, so intent was evaluated separately. The 550 requests contain
398 unique texts. The LLM (gemini-3.5-flash-lite) and an independent negation-aware keyword classifier both
ran on all of them and agree on 398/398. Both flag the same 10 non-deletion requests, case_533 to case_542:
unsubscribe, data export, profile correction, login help, invoice copy and an information request. These
are the same 10 cases that the activity log marks with `CLASSIFY_REQUEST ... non-deletion`.

## Custom evaluation cases

Command: `python code/evaluation/checks.py`. We wrote these five cases ourselves, each aimed at one failure
mode. Their expectations come from the problem statement or a labelled sample, not from the agent's own
output. All five pass.

| Case | What it tests | Expectation |
|---|---|---|
| custom_01 | A real customer (case_25's user) asks to stop newsletters and keep the account | NOT_APPLICABLE, `[]` |
| custom_02 | Sample case_12 re-sent in upper-case email and in German | Identical to the labelled case_12 answer |
| custom_03 | Deletion for g.silva76@gmail.com, next to a look-alike g.silva76.jr@gmail.com record | search_1874 never appears |
| custom_04 | Sample case_24 audited a month earlier (time travel) | No cited evidence captured after audit_at |
| custom_05 | Léonie's September request; a March log searched her email and got 404 for account_6523 | account_6523 must not appear |

custom_02 failed on its first run and led to iteration 11 below.

## Failures found and improvements made

Each row is a failure that the scorer or the checks exposed, the fix, and the sample scores after it.

| # | Failure observed | Fix | Status | Precision | Recall | Evidence |
|---|---|---|---|---|---|---|
| 1 | Baseline: link by identifier, latest state wins | n/a | 71.9% | 93.0% | 76.2% | 74.1% |
| 2 | Cited a bundle exported two days after the audit | Only evidence captured by `audit_at` may be cited; references (user_id) are not direct PII | 96.9% | 98.7% | 76.2% | 98.6% |
| 3 | A filtered JSON bundle "proved" retained audit records absent | SQL dumps = full-table captures (within `snapshot_scope`); JSON bundles only prove absence for users they cover | 96.9% | 99.4% | 81.9% | 100% |
| 4 | A decoy account linked through a 404 lookup | A failed search is not ownership | 96.9% | 100% | 78.8% | 100% |
| 5 | 410 primary-account deletions carry no identity | Rebuild the deletion worker's daily batches: day + status mapping (COMPLETE→SUCCESS, PARTIAL→PARTIAL, NOT_STARTED→FAILED, verified 128/129) + elimination; ambiguous slots left blank | 96.9% | 100% | 85.5% | 100% |
| 6 | Retained records only visible in later exports were dropped | Separate *state* (all lines timestamped before audit) from *citation* (prefer files captured by audit) | 96.9% | 100% | 88.1% | 100% |
| 7 | Hand-written cases where the worker finished minutes before reporting | Reported-time tie-breaker | 96.9% | 100% | 89.1% | 99.4% |
| 8 | Non-deletion requests audited as deletions | LLM intent classification with keyword cross-check; excluded from batches | 100% | 98.3% | 89.6% | 100% |
| 9 | Vision model invented `object_19` from a file name; used a printed creation date as observation time | Literal ids only; observation time = capture time | 96.9% | 99.4% | 89.1% | 99.4% |
| 10 | Uploaded PDF treated as a redactable message; vision identifiers not seen as PII | Category comes from record shape, not the vision label; PDF source = attachment | 100% | 100% | 90.7% | 100% |
| 11 | custom_02: a re-sent request competed with the original for one account | Same person, same day = one deletion job | 100% | 100% | 90.7% | 100% |
| 12 | Contract check: case_46 had no steps at all | Last resort: if nothing is linked, a pre-audit empty lookup for the person is the absence check | 100% | 100% | 90.7% | 100% |

We also tried two changes and reverted them because they lowered the scores: rebuilding non-account
resources from the worker batches (0 correct and 1 wrong on the samples), and trusting the vision model's
category labels.

## Known gaps

The missing 9.3% of recall sits in three places.

- Anonymised analytics profiles such as analytics_1895 and analytics_4566. After anonymisation they hold no
  identifier, and the only link is a deletion-worker action shared by several requests from the same day.
- Ambiguous primary accounts. When two requests with the same reported status were processed in one batch,
  nothing in the data says which account belongs to which request. We tested case id, request time, user
  id, email and resource id as possible orderings, and each came out close to 50/50.
- Two retained audit records, audit_record_6645 and audit_record_5569, that appear in no evidence file.

A guess in any of these places would add one false positive for each false negative it removed. The agent
leaves them out, which keeps precision at 100%.