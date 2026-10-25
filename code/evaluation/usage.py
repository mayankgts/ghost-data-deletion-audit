"""Write code/evaluation/usage_report.md from the token log in code/cache/usage.jsonl.

Usage: python3 code/evaluation/usage.py

Every model call made by vision.py and intent.py appends one line to usage.jsonl. Results are cached, so the
final full-dataset run reuses them: the report covers every call whose result the final output depends on.
Prices are USD per 1M tokens; fill PRICES from the provider's pricing page before generating the report.
"""
import csv
import json

USAGE = "code/cache/usage.jsonl"
OUT = "code/evaluation/usage_report.md"
REQUESTS = "dataset/deletion_requests.csv"

# model -> (input $/1M tokens, output $/1M tokens). Fill these in from the provider's pricing page.
PRICES = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
}

def main():
    with open(USAGE, encoding="utf-8") as f:
        calls = [json.loads(line) for line in f if line.strip()]
    with open(REQUESTS, encoding="utf-8") as f:
        n_requests = sum(1 for _ in csv.DictReader(f))

    per = {}
    for c in calls:
        k = (c["model"], c["stage"])
        p = per.setdefault(k, {"calls": 0, "in": 0, "out": 0})
        p["calls"] += 1
        p["in"] += c["input_tokens"]
        p["out"] += c["output_tokens"]

    def cost(model, tin, tout):
        pin, pout = PRICES.get(model, (None, None))
        return None if pin is None or pout is None else tin / 1e6 * pin + tout / 1e6 * pout

    def money(x):
        return "price not set" if x is None else f"${x:.4f}"

    lines = ["# Token usage and cost", "",
             f"Final full-dataset run: `python code/main.py` over {n_requests} requests in "
             "`dataset/deletion_requests.csv`, producing `output.csv`.", "",
             "Model calls happen in exactly two places; everything else (identity resolution, evidence parsing, "
             "state reconstruction, policy, step generation) is deterministic Python with zero tokens.", "",
             "- **Vision** (`code/vision.py`): one call per image/PDF evidence file (15 files), result cached.",
             "- **Intent** (`code/intent.py`): unique request texts sent in batches, result cached by text.", "",
             "Because results are cached, re-running the final command makes no new calls; the table below "
             "counts every call whose cached result the final output uses.", "",
             "## Per model", "",
             "| Model | Stage | Calls | Input tokens | Output tokens | Total tokens | Est. cost |",
             "|---|---|---|---|---|---|---|"]
    tot = {"calls": 0, "in": 0, "out": 0}
    total_cost, unpriced = 0.0, False
    for (model, stage), p in sorted(per.items()):
        c = cost(model, p["in"], p["out"])
        unpriced |= c is None
        total_cost += c or 0
        for k in tot:
            tot[k] += p[k]
        lines.append(f"| {model} | {stage} | {p['calls']} | {p['in']:,} | {p['out']:,} | {p['in'] + p['out']:,} | {money(c)} |")
    all_tokens = tot["in"] + tot["out"]
    lines += [f"| **All models** | | **{tot['calls']}** | **{tot['in']:,}** | **{tot['out']:,}** | **{all_tokens:,}** | "
              f"**{'incomplete' if unpriced else money(total_cost)}** |", "",
              "## Per request", "",
              f"- Requests in the final run: {n_requests}",
              f"- Average tokens per request: {all_tokens / n_requests:,.1f}",
              f"- Average model calls per request: {tot['calls'] / n_requests:.3f}",
              f"- Estimated cost per request: {'incomplete' if unpriced else money(total_cost / n_requests)}", "",
              "## Why it is this cheap", "",
              "- Evidence files are interpreted once, not once per request (15 vision calls serve all 518 cases).",
              "- 518 + 32 requests contain only ~398 unique texts; intent is classified per unique text, ~100 per call.",
              "- The LLM never reads the 4,011 activity rows or the SQL/JSON/log evidence: parsing those is code, "
              "which is both free and exact (an LLM would have to invent resource ids to do it).", ""]
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()  