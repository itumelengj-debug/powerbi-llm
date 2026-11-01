"""
rag_pbi.py - LEGACY plain-RAG harness (the original "attempt 2" knowledge layer).

Kept for comparison runs: it sends retrieved passages to a raw base model (default gemma4:26b)
with a short grounding prompt, *without* the powerbi brain, tools or verification. For real work
use powerbi_agent.py, which shares the same retriever (pbi_retrieval.HybridRetriever) and adds
the rulebook, gated tools and the verifier.

    python rag_pbi.py "Why does my YTD measure show blanks in December?"
    python rag_pbi.py --model powerbi --k 8 "..."
"""
import argparse
import json
import sys
import urllib.request

from powerbi_agent import OLLAMA_HOST, _safe_print, retrieve_context

DEFAULT_MODEL = "gemma4:26b"
K = 6
NUM_CTX = 8192

SYSTEM = (
    "You are a senior Microsoft Power BI consultant. Answer the user's question "
    "using ONLY the provided context. Cite the source tag in brackets (e.g. [S1]) "
    "where you use a fact. If the context does not contain the answer, say so "
    "plainly and do NOT invent DAX functions, formulas, or facts. Be concise and "
    "practical."
)


def build_prompt(query, chunks):
    ctx = "\n\n".join(f"[{c['id']}] (source: {c['source']})\n{c['text']}" for c in chunks)
    return (f"Context:\n{ctx}\n\nQuestion: {query}\n\n"
            "Answer using only the context above, citing sources in brackets.")


def chat(model, system, user_prompt, host=OLLAMA_HOST):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}],
        "stream": False,
        "think": False,
        "options": {"num_ctx": NUM_CTX, "temperature": 0.6, "repeat_penalty": 1.15},
    }
    req = urllib.request.Request(f"{host}/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=280) as r:
        return json.loads(r.read().decode())


def main(argv=None):
    ap = argparse.ArgumentParser(description="Legacy plain-RAG harness (no brain, no tools)")
    ap.add_argument("question", nargs="*")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--k", type=int, default=K)
    a = ap.parse_args(argv)
    query = " ".join(a.question).strip()
    if not query:
        ap.print_usage()
        return 2

    chunks = retrieve_context(query, k=a.k)
    print(f"[retrieved {len(chunks)} chunks for: {query!r}]")
    for c in chunks:
        print(f"  - [{c['id']}] {c['source']} (score {c.get('score')})")
    if not chunks:
        print("No relevant chunks retrieved.")
        return 1

    print(f"\n[asking model: {a.model}]\n")
    resp = chat(a.model, SYSTEM, build_prompt(query, chunks))
    print("=" * 70)
    _safe_print(resp.get("message", {}).get("content", "").strip())
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
