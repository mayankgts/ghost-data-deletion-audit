"""Decide whether each request actually asks for account/personal-data deletion.

Primary: one LLM call per batch of unique request texts (multilingual, handles negation such as
"remove me from marketing lists only" or "do not close the account"). Results are cached by text.
Fallback / cross-check: a negation-aware keyword classifier that needs no network.
"""
import hashlib
import json
import os
import re
import time

from vision import DEFAULT_MODELS, USAGE, _post, parse_json_text

CACHE = "code/cache/intent.json"
BATCH = 40

PROMPT = """You are the intake step of a privacy team. For each customer message below, decide whether the
customer is asking the company to DELETE / ERASE / CLOSE their account or personal data (a deletion request).

Not a deletion request: unsubscribing from marketing only, data access or copy/export requests, correcting or
updating details, login help, invoices, or any message that says to keep the account or not to delete.
A deletion request may also ask to keep anonymous statistics, retain backups or security records, or check
old email addresses; it is still a deletion request. A message asking to verify or confirm that an earlier
deletion really happened (e.g. "confirm the profile is gone") is also a deletion request. Messages may be
in any language.

Return ONLY JSON: {"results": [{"id": "<id>", "deletion_requested": true|false, "reason": "<max 12 words>"}]}

Messages:
"""

DELETE_WORDS = re.compile(r"delet|eras|remov|clos|is gone|suppr|elimin|exclu|lösch|apag|cancel|삭제|削除|删除|видал|удал", re.I)
NEGATED = re.compile(r"not (asking|requesting) for (deletion|erasure)|not a request to (erase|delete)|"
                     r"(do not|don't|without) (delete|close|remov|eras|chang)|keep the account|leave (the|my) "
                     r"(account|profile)|still (need|use|want)|continue using|marketing lists only|"
                     r"stop the promotional", re.I)


def keyword_intent(text):
    return bool(DELETE_WORDS.search(text)) and not NEGATED.search(text)


def key(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


def call_llm(prompt):
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    k = os.environ["LLM_API_KEY"]
    model = os.environ.get("LLM_MODEL_TEXT", os.environ.get("LLM_MODEL", DEFAULT_MODELS[provider]))
    if provider == "anthropic":
        r = _post("https://api.anthropic.com/v1/messages",
                  {"model": model, "max_tokens": 4096, "temperature": 0,
                   "messages": [{"role": "user", "content": prompt}]},
                  {"x-api-key": k, "anthropic-version": "2023-06-01"})
        return r["content"][0]["text"], r["usage"]["input_tokens"], r["usage"]["output_tokens"], model
    if provider == "openai":
        r = _post("https://api.openai.com/v1/chat/completions",
                  {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                   "messages": [{"role": "user", "content": prompt}]}, {"authorization": f"Bearer {k}"})
        return (r["choices"][0]["message"]["content"], r["usage"]["prompt_tokens"],
                r["usage"]["completion_tokens"], model)
    if provider == "gemini":
        r = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={k}",
                  {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}, {})
        u = r["usageMetadata"]
        return (r["candidates"][0]["content"]["parts"][0]["text"], u["promptTokenCount"],
                u.get("candidatesTokenCount", 0), model)
    raise ValueError(f"unknown LLM_PROVIDER {provider}")


def classify(texts):
    """texts: iterable of request texts -> {text_key: {"deletion_requested", "reason", "source"}}."""
    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    todo = {}
    for t in texts:
        if key(t) not in cache:
            todo[key(t)] = t
    # retry anything that previously fell back to keywords, now that a key may be available
    use_llm = bool(os.environ.get("LLM_API_KEY"))
    if use_llm:
        for k, v in list(cache.items()):
            if v.get("source") == "keyword" and k not in todo:
                todo[k] = v["text"]
    items = list(todo.items())
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        got = {}
        if use_llm:
            body = "\n".join(json.dumps({"id": k, "text": t}, ensure_ascii=False) for k, t in chunk)
            for attempt in range(6):  # free tiers rate-limit: back off 10s, 20s, 40s...
                try:
                    text, tin, tout, model = call_llm(PROMPT + body)
                    got = {r["id"]: r for r in parse_json_text(text)["results"]}
                    with open(USAGE, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"stage": "intent", "texts": len(chunk), "model": model,
                                            "input_tokens": tin, "output_tokens": tout}) + "\n")
                    break
                except Exception as e:
                    wait = 10 * 2 ** attempt
                    print(f"  intent batch {i // BATCH + 1}: {e}; retrying in {wait}s")
                    time.sleep(wait)
            else:
                print(f"  intent batch {i // BATCH + 1}: giving up, keyword fallback")
            time.sleep(4)  # stay under requests-per-minute limits
        for k, t in chunk:
            kw = keyword_intent(t)
            if k in got:
                cache[k] = {"deletion_requested": bool(got[k]["deletion_requested"]), "text": t,
                            "reason": got[k].get("reason", ""), "source": "llm",
                            "keyword_agrees": kw == bool(got[k]["deletion_requested"])}
            else:
                cache[k] = {"deletion_requested": kw, "text": t, "reason": "keyword fallback", "source": "keyword"}
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, ensure_ascii=False)
    return cache


def is_deletion(cache, text):
    return cache[key(text)]["deletion_requested"]


if __name__ == "__main__":
    import csv
    reqs = []
    for p in ("dataset/deletion_requests.csv", "dataset/sample_deletion_requests.csv"):
        with open(p, encoding="utf-8") as f:
            reqs += list(csv.DictReader(f))
    cache = classify(r["request_text"] for r in reqs)
    na = [r["case_id"] for r in reqs if not is_deletion(cache, r["request_text"])]
    unique = len({key(r["request_text"]) for r in reqs})
    disagree = [v for v in cache.values() if v.get("keyword_agrees") is False]
    print(f"{len(reqs)} requests, {unique} unique texts; NOT_APPLICABLE: {len(na)} -> {na}")
    print(f"LLM/keyword disagreements: {len(disagree)}")