"""gen_visual.py — generate Deneb-ready Vega/Vega-Lite specs adapted to your fields."""
import json
import re
import sys
import urllib.request
from pathlib import Path

from agent.rag import DocumentStore
from agent import config

def _safe_print(text):
    """Print without crashing the Windows cp1252 console on unicode."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))



DEFAULT_MODEL = "gemma4:26b"
NUM_CTX = 8192
INDEXED_DIR = Path(__file__).resolve().parent / "visual_specs" / "indexed"

SYSTEM = (
    "You are a senior Power BI / Deneb developer. Given a Vega/Vega-Lite spec "
    "template and the user's field names, produce a Deneb-ready spec adapted to the "
    "user's data. Return ONLY a valid JSON object, no markdown fences, no commentary. "
    "HARD RULES:\n"
    "1. The spec MUST read its data from Deneb's binding: "
    "\"data\": {\"name\": \"dataset\"}. NEVER include inline \"values\": [...] "
    "sample data — remove every hardcoded data array and replace it with the dataset "
    "binding.\n"
    "2. Map the user's exact field names into the encodings (x, y, color, size, "
    "source/target for flows, etc.).\n"
    "3. Preserve the template's visual structure, marks, scales, and styling.\n"
    "4. Deneb fields become columns in the \"dataset\" object — reference them by "
    "their field name (e.g. \"field\": \"amount\")."
)


def retrieve(store, query, k=3):
    return store.search(query, k=k)


def load_full_spec(source_name):
    p = INDEXED_DIR / source_name
    if not p.exists():
        return None
    md = p.read_text(encoding="utf-8")
    m = re.search(r"```json\s*\n(.*?)\n```", md, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def chat(model, system, user_prompt, host=None):
    host = host or config.OLLAMA_HOST.rstrip("/")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "think": False,
        "options": {"num_ctx": NUM_CTX, "temperature": 0.6, "repeat_penalty": 1.15},
    }
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=280) as r:
        return json.loads(r.read().decode())


def parse_json_from_text(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
    return None


def has_dataset_binding(spec):
    txt = json.dumps(spec)
    return re.search(r'"name"\s*:\s*"dataset"', txt) is not None


def main():
    args = sys.argv[1:]
    model = DEFAULT_MODEL
    k = 3
    parts = []
    i = 0
    while i < len(args):
        if args[i] == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        elif args[i] == "--k" and i + 1 < len(args):
            k = int(args[i + 1])
            i += 2
        else:
            parts.append(args[i])
            i += 1
    request = " ".join(parts).strip()
    if not request:
        print("Usage: python gen_visual.py \"sankey of client flows, fields: source, target, amount\"")
        return

    store = DocumentStore()
    chunks = retrieve(store, request, k)
    if not chunks:
        print("No matching visual spec found in the index.")
        return

    best = chunks[0]
    print(f"[matched spec: {best['source']} (score {best.get('score')})]")
    spec = load_full_spec(best["source"])
    if spec is None:
        for c in chunks[1:]:
            spec = load_full_spec(c["source"])
            if spec is not None:
                print(f"[loaded full spec from: {c['source']}]")
                break
    if spec is None:
        print("Could not load the full spec from disk.")
        return

    print(f"[adapting spec to your fields via {model}...]\n")
    user_prompt = (
        f"Visual request: {request}\n\n"
        f"Template spec (Vega/Vega-Lite):\n{json.dumps(spec)}\n\n"
        f"Adapt this spec to the user's data following the HARD RULES. "
        f"Return ONLY the adapted JSON object."
    )
    resp = chat(model, SYSTEM, user_prompt)
    answer = resp.get("message", {}).get("content", "")

    adapted = parse_json_from_text(answer)
    print("=" * 70)
    if adapted is None:
        print("[model did not return parseable JSON - showing raw reply]")
        _safe_print(answer.strip())
        print("=" * 70)
        return

    print(json.dumps(adapted, indent=2))
    print("=" * 70)

    if has_dataset_binding(adapted):
        print("[CHECK] OK: bound to Deneb dataset.")
    else:
        print("[CHECK] WARNING: not bound to dataset. Edit the spec to use "
              "\"data\": {\"name\": \"dataset\"} and map your fields.")

    print("\nBUILD NOTE: add a Deneb visual in Power BI, paste the spec, and bind "
          "your fields in the Deneb data roles (\"dataset\" = your fields).")
    print("=" * 70)


if __name__ == "__main__":
    main()
