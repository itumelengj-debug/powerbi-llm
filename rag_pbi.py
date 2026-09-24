"""
Power BI consultant RAG harness (layer 2).

Reuses the app's OWN DocumentStore + embed_texts (agent/rag.py) — no rebuild.
Flow: embed the question → retrieve top-k chunks from the 1,326-doc Power BI
index → build a grounded prompt that instructs the model to answer ONLY from
the retrieved context and cite sources → call a local Ollama chat model.

Run from the repo root (so `agent` is importable):
    python rag_pbi.py "Why does my YTD measure show blanks in December?"

Optional flags:
    --model <name>   local Ollama model (default: gemma4:26b)
    --k <n>          number of chunks to retrieve (default 6)
"""
import json
import sys
import urllib.request

from agent.rag import DocumentStore
from agent import config

def _safe_print(text):
    """Print without crashing the Windows cp1252 console on unicode."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))



DEFAULT_MODEL = "gemma4:26b"
K = 6
NUM_CTX = 8192

SYSTEM = (
    "You are a senior Microsoft Power BI consultant. Answer the user's question "
    "using ONLY the provided context. Cite the source document name in brackets "
    "where you use a fact. If the context does not contain the answer, say so "
    "plainly and do NOT invent DAX functions, formulas, or facts. Be concise and "
    "practical."
)


def retrieve(store, query, k):
    return store.search(query, k=k)


def build_prompt(query, chunks):
    ctx = "\n\n".join(
        f"[{i+1}] (source: {c['source']})\n{c['text']}"
        for i, c in enumerate(chunks)
    )
    return (
        f"Context:\n{ctx}\n\n"
        f"Question: {query}\n\n"
        f"Answer using only the context above, citing sources in brackets."
    )


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


def main():
    args = sys.argv[1:]
    model = DEFAULT_MODEL
    k = K
    query_parts = []
    i = 0
    while i < len(args):
        if args[i] == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        elif args[i] == "--k" and i + 1 < len(args):
            k = int(args[i + 1])
            i += 2
        else:
            query_parts.append(args[i])
            i += 1
    query = " ".join(query_parts).strip()
    if not query:
        print("Usage: python rag_pbi.py [--model M] [--k N] \"your question\"")
        return

    store = DocumentStore()
    chunks = retrieve(store, query, k)
    print(f"[retrieved {len(chunks)} chunks for: {query!r}]")
    for c in chunks:
        print(f"  - {c['source']} (score {c.get('score')})")
    if not chunks:
        print("No relevant chunks retrieved.")
        return

    print(f"\n[asking model: {model}]\n")
    prompt = build_prompt(query, chunks)
    resp = chat(model, SYSTEM, prompt)
    answer = resp.get("message", {}).get("content", "")
    print("=" * 70)
    _safe_print(answer.strip())
    print("=" * 70)


if __name__ == "__main__":
    main()
