# Token usage and cost

Final full-dataset run: `python code/main.py` over 518 requests in `dataset/deletion_requests.csv`, producing `output.csv`.

Model calls happen in exactly two places; everything else (identity resolution, evidence parsing, state reconstruction, policy, step generation) is deterministic Python with zero tokens.

- **Vision** (`code/vision.py`): one call per image/PDF evidence file (15 files), result cached.
- **Intent** (`code/intent.py`): unique request texts sent in batches, result cached by text.

Because results are cached, re-running the final command makes no new calls; the table below counts every call whose cached result the final output uses.

## Per model

| Model | Stage | Calls | Input tokens | Output tokens | Total tokens | Est. cost |
|---|---|---|---|---|---|---|
| gemini-2.5-flash | intent | 6 | 15,839 | 12,408 | 28,247 | $0.0358 |
| gemini-2.5-flash | vision | 15 | 13,929 | 12,757 | 26,686 | $0.0361 |
| gemini-3.5-flash | vision | 1 | 1,733 | 101 | 1,834 | $0.0035 |
| gemini-3.5-flash-lite | intent | 11 | 26,312 | 19,559 | 45,871 | $0.0568 |
| **All models** | | **33** | **57,813** | **44,825** | **102,638** | **$0.1321** |

## Per request

- Requests in the final run: 518
- Average tokens per request: 198.1
- Average model calls per request: 0.064
- Estimated cost per request: $0.0003

## Why it is this cheap

- Evidence files are interpreted once, not once per request (15 vision calls serve all 518 cases).
- 518 + 32 requests contain only ~398 unique texts; intent is classified per unique text, ~100 per call.
- The LLM never reads the 4,011 activity rows or the SQL/JSON/log evidence: parsing those is code, which is both free and exact (an LLM would have to invent resource ids to do it).

## Notes

- The log contains every successful call made while building the final caches, including some that were
  later superseded. One evidence file was re-extracted after a prompt fix, and the intent batches were re-run
  after a change of provider. The figures are therefore an upper bound: a clean run needs 15 vision calls
  and 4 intent calls.
- The run used the Gemini API free tier, so the actual charge was $0. The cost column shows what these token
  counts would cost at Google's paid-tier list prices in October 2026.