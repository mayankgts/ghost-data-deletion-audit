# Ghost Data Still Exists: deletion audit agent

For each request, the agent first decides whether the person asked for deletion at all. For deletion
requests it works out which records still identify the person at the audit time, applies
`deletion_rules.csv`, and writes the ordered findings, remediation and verification steps.

## Run

Python 3.10 or later, standard library only, so nothing needs installing. Run every command from the
repository root.

```
python code/vision.py            # once: images/PDFs -> code/cache/vision/*.json (needs an API key)
python code/evidence.py          # all evidence -> code/cache/observations.json
python code/main.py              # dataset/deletion_requests.csv -> output.csv
```

Evaluation:

```
python code/main.py --samples && python code/evaluation/main.py -v   # scores vs sample_output.csv
python code/evaluation/checks.py                                     # contract checks + custom cases
python code/evaluation/usage.py                                      # writes usage_report.md
```

Model access comes from environment variables and never appears in the code: `LLM_PROVIDER` (gemini,
anthropic or openai), `LLM_API_KEY`, and optionally `LLM_MODEL` for vision and `LLM_MODEL_TEXT` for intent.
Vision and intent results are cached in `code/cache/`, so `main.py` runs offline in about 10 seconds.
Without a key, intent classification falls back to the keyword classifier.

## Architecture

| File | Role |
|---|---|
| `identity.py` | Email → user_id → every current and historical email, phone and SHA-256 email hash |
| `evidence.py` | Normalises JSON, SQL (executed in SQLite), 7 log formats (joined on trace_id), TXT and cached vision output into one observation table |
| `vision.py` | One vision-model call per image/PDF, structured JSON output, cached |
| `intent.py` | LLM classification of request intent (per unique text, batched), keyword cross-check |
| `state.py` | Resource state at `audit_at` and which evidence to cite |
| `audit.py` | Linking, policy (`deletion_rules.csv`), step generation and ordering |
| `batch.py` | Reconstructs the deletion worker's daily batches for records that carry no identity |
| `main.py` | Pipeline entry point |
| `evaluation/` | Scorer, contract checks, custom cases, usage report, written reports |

The two prompts are the `PROMPT` constants in `vision.py` and `intent.py`.

## Design decisions

The agent treats `reported_status` and the results in `system_activity.csv` as claims. A record's state
comes only from evidence files, and a step may only cite a file that had been captured by the case's
`audit_at`.

Models handle the two jobs that need them: reading screenshots and PDFs, and understanding what a request
asks for. Logs, SQL dumps and JSON exports are parsed in code, which keeps every resource id exact. That is
why step precision on the samples is 100%.

Nothing in the code refers to a specific case, user or resource.

When the evidence cannot settle which request a record belongs to, the agent leaves the step out. A wrong
step would count against precision and recall at once, while a missing one only costs recall.