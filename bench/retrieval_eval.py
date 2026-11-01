r"""
retrieval_eval.py - measure the knowledge layer on its own: does the right passage come back?

Gold file: JSON lines {"q": question, "expect": [substring, ...]}. A hit is a retrieved passage whose
source (URL or document name) contains any expected substring (case-insensitive). Reports recall@k
and MRR@k, and compares reranking settings so you can see whether a change to the retriever helps.

    python bench\retrieval_eval.py                                   # uses retrieval_gold.example.jsonl
    python bench\retrieval_eval.py --gold my_gold.jsonl --k 6 --project C:\PBI\FX
    python bench\retrieval_eval.py --embed nomic-embed-text          # add an embedding-rerank variant

Edit the example gold set to match your corpus: the substrings must appear in your sources.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent / "agent-jo")]


def load_gold(path: str) -> list:
    out = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(json.loads(ln))
    return out


def evaluate(retriever, gold: list, k: int) -> dict:
    hits, rr, misses = 0, 0.0, []
    for g in gold:
        res = retriever.search(g["q"], k=k)
        exp = [e.lower() for e in g["expect"]]
        rank = next((i for i, c in enumerate(res, 1) if any(e in str(c["source"]).lower() for e in exp)), None)
        if rank:
            hits += 1
            rr += 1.0 / rank
        else:
            misses.append(g["q"])
    n = max(len(gold), 1)
    return {"recall": round(hits / n, 3), "mrr": round(rr / n, 3), "misses": misses}


def main(argv=None) -> int:
    import powerbi_agent as pa
    from pbi_retrieval import HybridRetriever, OllamaEmbedder

    ap = argparse.ArgumentParser(description="Retrieval recall@k / MRR@k")
    ap.add_argument("--gold", default=str(HERE / "retrieval_gold.example.jsonl"))
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--project", action="append", default=[])
    ap.add_argument("--embed", default="", help="Ollama embedding model for an extra variant")
    a = ap.parse_args(argv)

    gold = load_gold(a.gold)
    base = pa.get_retriever(a.project, embed_model="")
    if not base.sources:
        print("No retrieval source available: run refresh_docs.py or run inside Agent Jo.")
        return 2
    variants = {
        "pool=k (no rerank headroom)": HybridRetriever(base.sources, pool=a.k, project_slots=0),
        "pool=20 rerank": HybridRetriever(base.sources, pool=20),
        "pool=40 rerank": HybridRetriever(base.sources, pool=40),
    }
    if a.embed:
        variants["pool=20 + embeddings"] = HybridRetriever(base.sources, pool=20,
                                                           embed=OllamaEmbedder(a.embed, pa.OLLAMA_HOST))
    print(f"{len(gold)} questions, k={a.k}, sources: {[s[0] for s in base.sources]}\n")
    print(f"{'variant':<30} {'recall@k':>9} {'MRR@k':>7}")
    worst = None
    for name, r in variants.items():
        m = evaluate(r, gold, a.k)
        print(f"{name:<30} {m['recall']:>9.1%} {m['mrr']:>7.3f}")
        worst = worst or m
    if worst and worst["misses"]:
        print("\nMissed by the first variant:\n  " + "\n  ".join(worst["misses"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
