"""Vision pass: turn screenshots (PNG) and documents (PDF) into structured JSON, once, cached.

Provider is chosen with env vars (no SDKs needed, plain HTTPS):
    LLM_PROVIDER = anthropic | openai | gemini
    LLM_API_KEY  = your key
    LLM_MODEL    = optional override of the default model
"""
import base64
import json
import os
import time
import urllib.request

DATA = "dataset"
CACHE = "code/cache/vision"
USAGE = "code/cache/usage.jsonl"

DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-4.1", "gemini": "gemini-2.5-flash"}

PROMPT = """You are transcribing an internal admin screenshot or document from an e-commerce company
for a privacy deletion audit. Transcribe exactly what is visible. Never guess or invent values.

Return ONLY a JSON object with this shape:
{
  "screen_title": "page or document title",
  "captured_time": "ISO-8601 time shown on the page if any, else null",
  "subject": {"user_ids": [], "emails": [], "phones": [], "names": [], "resource_ids": []},
  "records": [
    {
      "resource_id": "exact id such as account_5762, support_9538, search_428, backup_9287, data_flow_1121,
                      analytics_7438, export_1260, audit_record_003, tombstone_17, object_19, marketing_001",
      "system_hint": "which service/console it belongs to, e.g. account_service, search_service",
      "record_type": "e.g. USER_PROFILE, SEARCH_INDEX_ENTRY, SUPPORT_ATTACHMENT, BACKUP_ARCHIVE, DATA_FLOW",
      "status": "status text exactly as shown (e.g. Active, Enabled, Restorable, Deleted, Redacted), else null",
      "time": "ISO-8601 time shown for this record if any, else null",
      "identifiers": ["every email, phone, user_id, email hash, name shown ON THE SAME ROW/PANEL as this resource"],
      "flags": {"restore_allowed": null, "enabled": null, "download_available": null,
                "legal_hold": null, "retain_until": null, "belongs_only_to_subject": null},
      "notes": "short note on anything else relevant (other people in the record, shared context)"
    }
  ],
  "other_rows": [{"reference": "ids without the resource_id pattern, e.g. ORD-..., CS-...",
                  "identifiers": [], "status": null, "notes": null}]
}

Rules:
- Only use resource ids that are literally printed on the page. Never derive an id from a file name,
  title or number (a file called delivery_confirmation_19.pdf does NOT mean object_19).
- Emit one record for EVERY resource id visible anywhere: header, tables, side panels, activity feeds.
- A row's identifiers are only those on that row or in that row's detail panel. If the whole page is
  about one customer (header shows their name/user_id/email) and a resource is shown as theirs,
  include the customer's identifiers for it.
- Similar-looking emails are different people (g.silva76@ vs g.silva76.jr@). Copy them exactly.
- Use null for anything not visible."""


def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def call_model(path):
    """Send one file to the configured provider. Returns (text, input_tokens, output_tokens, model)."""
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    key = os.environ["LLM_API_KEY"]
    model = os.environ.get("LLM_MODEL", DEFAULT_MODELS[provider])
    data = _b64(path)
    is_pdf = path.endswith(".pdf")
    mime = "application/pdf" if is_pdf else "image/png"

    if provider == "anthropic":
        part = {"type": "document" if is_pdf else "image", "source": {"type": "base64", "media_type": mime, "data": data}}
        r = _post("https://api.anthropic.com/v1/messages",
                  {"model": model, "max_tokens": 4096, "temperature": 0,
                   "messages": [{"role": "user", "content": [part, {"type": "text", "text": PROMPT}]}]},
                  {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return r["content"][0]["text"], r["usage"]["input_tokens"], r["usage"]["output_tokens"], model

    if provider == "openai":
        if is_pdf:
            part = {"type": "file", "file": {"filename": os.path.basename(path), "file_data": f"data:{mime};base64,{data}"}}
        else:
            part = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
        r = _post("https://api.openai.com/v1/chat/completions",
                  {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                   "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT}, part]}]},
                  {"authorization": f"Bearer {key}"})
        u = r["usage"]
        return r["choices"][0]["message"]["content"], u["prompt_tokens"], u["completion_tokens"], model

    if provider == "gemini":
        r = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                  {"contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": data}}, {"text": PROMPT}]}],
                   "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}, {})
        u = r["usageMetadata"]
        return (r["candidates"][0]["content"]["parts"][0]["text"],
                u["promptTokenCount"], u.get("candidatesTokenCount", 0), model)

    raise ValueError(f"unknown LLM_PROVIDER {provider}")


def parse_json_text(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def extract(path, file_id, retries=3):
    """Cached: each file is sent to the model at most once across all runs."""
    out = f"{CACHE}/{file_id}.json"
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            return json.load(f)
    for attempt in range(retries):
        try:
            text, tin, tout, model = call_model(path)
            result = parse_json_text(text)
            break
        except Exception as e:  # network hiccup or malformed JSON: retry
            print(f"  {file_id} attempt {attempt + 1} failed: {e}")
            time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"vision extraction failed for {path}")
    os.makedirs(CACHE, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    with open(USAGE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"stage": "vision", "file_id": file_id, "model": model,
                            "input_tokens": tin, "output_tokens": tout}) + "\n")
    return result


def run_all():
    import csv
    with open(f"{DATA}/evidence_files.csv", encoding="utf-8") as f:
        files = [r for r in csv.DictReader(f) if r["file_path"].endswith((".png", ".pdf"))]
    for r in files:
        res = extract(f"{DATA}/{r['file_path']}", r["file_id"])
        print(r["file_id"], r["file_path"].rsplit("/", 1)[-1], "->", len(res.get("records", [])), "records")


if __name__ == "__main__":
    run_all()