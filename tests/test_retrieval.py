"""DocsIndex, ProjectIndex and HybridRetriever."""
from datetime import date

from pbi_retrieval import DocsIndex, HybridRetriever, ProjectIndex, chunk_text, terms


class ListStore:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, k=20):
        return self.hits[:k]


def test_terms_and_chunking():
    assert terms("How do I write a YTD measure in Power BI?") == ["ytd", "measure"]
    text = "intro para\n\n```dax\nX =\n\nSUM ( T[a] )\n```\n\n" + ("word " * 400)
    chunks = chunk_text(text, size=300)
    assert any("```dax\nX =\n\nSUM ( T[a] )\n```" in c for c in chunks)      # code block kept whole
    assert len(chunks) > 3


def test_docs_index_roundtrip(tmp_path):
    db = DocsIndex(tmp_path / "d.sqlite")
    n = db.upsert_page("https://learn.microsoft.com/en-us/dax/divide-function-dax", "DIVIDE function",
                       "DIVIDE performs division and returns alternate result on division by 0.", "2026-08-01",
                       "abc", ["https://learn.microsoft.com/en-us/dax/x"])
    assert n == 1
    db.upsert_page("https://learn.microsoft.com/en-us/dax/sum", "SUM", "Adds all the numbers in a column.", "2025-01-01")
    hits = db.search("safe division by zero")
    assert hits[0]["source"].endswith("divide-function-dax") and hits[0]["date"] == "2026-08-01"
    assert db.links("https://learn.microsoft.com/en-us/dax/divide-function-dax") == ["https://learn.microsoft.com/en-us/dax/x"]
    # re-upsert replaces chunks rather than duplicating them
    db.upsert_page("https://learn.microsoft.com/en-us/dax/sum", "SUM", "Adds numbers. Updated.", "2026-09-01")
    assert db.stats()["chunks"] == 2 and db.stats()["pages"] == 2
    assert db.search("") == [] and db.search("the and") == []


def test_project_index_finds_real_names(sandbox):
    idx = ProjectIndex([str(sandbox)])
    hits = idx.search("notional measure format")
    assert hits and hits[0]["source"].startswith("project:")
    assert any("Notional (ZAR)" in h["text"] for h in hits)
    assert any("visual" in h["text"] and "card" in h["text"] for h in idx.search("card visual overview"))
    # rescans when files change
    t = sandbox / "FX.SemanticModel" / "definition" / "tables" / "Trades.tmdl"
    t.write_text(t.read_text() + "\n\tmeasure 'Hedge Ratio' = 1\n", encoding="utf-8")
    import os, time
    os.utime(t, (time.time() + 5, time.time() + 5))
    assert any("Hedge Ratio" in h["text"] for h in idx.search("hedge ratio"))


def test_hybrid_prefers_fresh_and_relevant_and_reserves_project_slots():
    old = {"source": "learn/old-ytd", "text": "TOTALYTD year to date measure guidance", "score": 10, "date": "2019-01-01"}
    new = {"source": "learn/new-ytd", "text": "TOTALYTD year to date measure guidance", "score": 10, "date": "2026-09-01"}
    noise = [{"source": f"learn/noise{i}", "text": "gateway cluster setup", "score": 11 - i} for i in range(6)]
    proj = {"source": "project:FX.SemanticModel/_Measures.tmdl", "text": "measure 'Notional YTD'", "score": 1}
    r = HybridRetriever([("learn", ListStore([old, new] + noise), 1.0), ("project", ListStore([proj]), 0.9)],
                        today=date(2026, 10, 1))
    out = r.search("YTD measure", k=4)
    srcs = [c["source"] for c in out]
    assert srcs.index("learn/new-ytd") < srcs.index("learn/old-ytd")
    assert "project:FX.SemanticModel/_Measures.tmdl" in srcs
    assert [c["id"] for c in out] == ["S1", "S2", "S3", "S4"]


def test_hybrid_dedupes_and_survives_broken_source():
    class Broken:
        def search(self, q, k=20):
            raise RuntimeError("index locked")
    dup = {"source": "a", "text": "same text here about DAX", "score": 1}
    r = HybridRetriever([("x", ListStore([dup, dict(dup)]), 1.0), ("bad", Broken(), 1.0), ("none", None, 1.0)])
    assert len(r.search("DAX", k=5)) == 1


def test_hybrid_embedding_rerank(llm):
    from pbi_retrieval import OllamaEmbedder
    hits = [{"source": "s1", "text": "gateway", "score": 2}, {"source": "s2", "text": "dax measure", "score": 1}]
    r = HybridRetriever([("x", ListStore(hits), 1.0)], embed=OllamaEmbedder("emb", llm.url))
    out = r.search("dax", k=2)
    assert {c["source"] for c in out} == {"s1", "s2"}
    assert any(p == "/api/embed" for p, _ in llm.requests)
