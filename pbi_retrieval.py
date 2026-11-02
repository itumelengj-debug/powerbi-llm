"""
pbi_retrieval.py - the knowledge layer: docs index, project index and a reranking retriever.

Three sources, merged and reranked into the [S1]..[Sn] passages the brain cites:

  - Agent Jo's DocumentStore (agent.rag) when that package is importable (the 1,326-doc corpus).
  - DocsIndex: a local SQLite FTS5 index of Microsoft Learn pages, kept current by refresh_docs.py.
    Every passage carries the page's ms.date so the model can prefer the newest guidance.
  - ProjectIndex: the user's own PBIP project (TMDL tables/measures, PBIR pages/visuals), so real
    table, column and measure names are in context without a read_project round-trip.

HybridRetriever pulls a wide candidate pool (default 20) from every source, normalises scores per
source, reranks on (source score + query-term coverage [or embedding similarity] + freshness),
de-duplicates, reserves a couple of slots for project passages, and returns the top k.

Standard library only. Optional: OllamaEmbedder for embedding-based reranking.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

DEFAULT_DOCS_DB = Path.home() / ".powerbi-llm" / "docs.sqlite"

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "how", "what", "why", "does", "do", "is", "are", "my",
    "can", "you", "your", "use", "using", "into", "when", "which", "show", "shows", "get", "give", "need", "want",
    "make", "should", "would", "will", "there", "their", "them", "then", "than", "but", "not", "all", "any", "per",
    "each", "only", "add", "write", "create", "power", "bi",
}


def terms(text: str) -> list:
    """Lower-cased content words (deduplicated, order kept)."""
    seen, out = set(), []
    for w in _WORD.findall(text or ""):
        w = w.lower()
        if len(w) > 2 and w not in _STOP and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _fts_query(text: str) -> str:
    ts = terms(text)
    return " OR ".join(f'"{t}"' for t in ts[:24])


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list:
    """Split on paragraph boundaries into ~size-character chunks, keeping fenced code blocks whole."""
    paras, buf, out = re.split(r"\n\s*\n", text or ""), "", []
    merged, in_code = [], False
    for p in paras:  # re-join paragraphs that split a ``` block
        if in_code:
            merged[-1] += "\n\n" + p
        else:
            merged.append(p)
        if p.count("```") % 2 == 1:
            in_code = not in_code
    pieces = []
    for p in merged:  # hard-split oversized prose paragraphs (long tables, run-on text) at whitespace
        if len(p) > size * 1.5 and "```" not in p:
            words, cur = p.split(), ""
            for w in words:
                if cur and len(cur) + len(w) + 1 > size:
                    pieces.append(cur)
                    cur = w
                else:
                    cur = f"{cur} {w}" if cur else w
            if cur:
                pieces.append(cur)
        else:
            pieces.append(p)
    for p in pieces:
        p = p.strip()
        if not p:
            continue
        if buf and len(buf) + len(p) + 2 > size:
            out.append(buf)
            buf = buf[-overlap:].split(" ", 1)[-1] + "\n\n" + p if overlap else p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf:
        out.append(buf)
    return out


def _parse_date(s) -> Optional[date]:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


# --------------------------------------------------------------------------- docs index


class DocsIndex:
    """SQLite FTS5 index of documentation pages (filled by refresh_docs.py)."""

    def __init__(self, path=DEFAULT_DOCS_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS pages (
                url TEXT PRIMARY KEY, title TEXT, ms_date TEXT, fetched TEXT, sha TEXT, links TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                title, text, url UNINDEXED, ms_date UNINDEXED, tokenize = 'porter unicode61');
        """)

    def close(self):
        self.db.close()

    def page(self, url: str) -> Optional[dict]:
        r = self.db.execute("SELECT url, title, ms_date, fetched, sha FROM pages WHERE url = ?", (url,)).fetchone()
        return dict(zip(("url", "title", "ms_date", "fetched", "sha"), r)) if r else None

    def links(self, url: str) -> list:
        r = self.db.execute("SELECT links FROM pages WHERE url = ?", (url,)).fetchone()
        return json.loads(r[0]) if r and r[0] else []

    def upsert_page(self, url: str, title: str, text: str, ms_date: str = "", sha: str = "",
                    links: Optional[list] = None) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db:
            self.db.execute("DELETE FROM chunks WHERE url = ?", (url,))
            self.db.execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?, ?)",
                            (url, title, ms_date, now, sha, json.dumps(links or [])))
            parts = chunk_text(text)
            self.db.executemany("INSERT INTO chunks (title, text, url, ms_date) VALUES (?, ?, ?, ?)",
                                [(title, c, url, ms_date) for c in parts])
        return len(parts)

    def touch(self, url: str, links: Optional[list] = None):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db:
            self.db.execute("UPDATE pages SET fetched = ?, links = COALESCE(?, links) WHERE url = ?",
                            (now, json.dumps(links) if links is not None else None, url))

    def stats(self) -> dict:
        pages = self.db.execute("SELECT COUNT(*), MIN(ms_date), MAX(fetched) FROM pages").fetchone()
        chunks = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"pages": pages[0], "chunks": chunks, "last_fetched": pages[2]}

    def search(self, query: str, k: int = 20) -> list:
        q = _fts_query(query)
        if not q:
            return []
        rows = self.db.execute(
            "SELECT url, title, ms_date, text, bm25(chunks, 2.0, 1.0) AS s FROM chunks WHERE chunks MATCH ? "
            "ORDER BY s LIMIT ?", (q, k)).fetchall()
        return [{"source": url, "title": title, "date": ms_date or "", "text": text, "score": -s}
                for url, title, ms_date, text, s in rows]


# --------------------------------------------------------------------------- project index

_TMDL_OBJ = re.compile(r"^\t(measure|column|calculationGroup|calculationItem|hierarchy|partition)\s+"
                       r"('(?:[^']|'')+'|[^\s=]+)")


class ProjectIndex:
    """In-memory FTS index over PBIP projects under the given roots. Re-scans when files change."""

    def __init__(self, roots: Iterable[str], max_files: int = 5000):
        self.roots = [Path(r).expanduser() for r in roots if r]
        self.max_files = max_files
        self._stamp = None
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute("CREATE VIRTUAL TABLE p USING fts5(text, source UNINDEXED, tokenize = 'porter unicode61')")

    def _files(self) -> list:
        out = []
        for root in self.roots:
            if not root.exists():
                continue
            out += list(root.rglob("*.SemanticModel/definition/**/*.tmdl"))
            out += list(root.rglob("*.Report/definition/pages/**/*.json"))
            if len(out) > self.max_files:
                break
        return out[: self.max_files]

    def refresh(self) -> int:
        files = self._files()
        stamp = (len(files), max((f.stat().st_mtime for f in files), default=0))
        if stamp == self._stamp:
            return 0
        self._stamp = stamp
        rows = []
        for f in files:
            try:
                rows += self._chunks(f)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
        with self.db:
            self.db.execute("DELETE FROM p")
            self.db.executemany("INSERT INTO p (text, source) VALUES (?, ?)", rows)
        return len(rows)

    def _rel(self, f: Path) -> str:
        for r in self.roots:
            try:
                return f.relative_to(r).as_posix()
            except ValueError:
                continue
        return f.name

    def _chunks(self, f: Path) -> list:
        rel = self._rel(f)
        if f.suffix == ".tmdl":
            lines = f.read_text(encoding="utf-8").splitlines()
            head = lines[0].strip() if lines else f.stem
            blocks, cur, name = [], [], None
            for ln in lines:
                m = _TMDL_OBJ.match(ln)
                if m:
                    if cur and name:
                        blocks.append((name, cur))
                    name, cur = f"{m.group(1)} {m.group(2)}", [ln]
                elif name:
                    cur.append(ln)
            if cur and name:
                blocks.append((name, cur))
            if not blocks:
                return [("\n".join(lines)[:2000], f"project:{rel}")]
            out = [(f"{head}\n" + "\n".join(b)[:1500], f"project:{rel}#{n}") for n, b in blocks]
            summary = f"{head}\nobjects: " + ", ".join(n for n, _ in blocks)
            return [(summary[:2000], f"project:{rel}")] + out
        doc = json.loads(f.read_text(encoding="utf-8-sig"))
        if f.name == "visual.json":
            v = doc.get("visual") or {}
            fields = []
            for role, spec in ((v.get("query") or {}).get("queryState") or {}).items():
                for pr in (spec or {}).get("projections", []):
                    fields.append(f"{role}: {pr.get('queryRef')}")
            text = f"visual {doc.get('name')} type {v.get('visualType')} " + "; ".join(fields)
        elif f.name == "page.json":
            text = f"page {doc.get('name')} '{doc.get('displayName')}' {doc.get('width')}x{doc.get('height')}"
        else:
            text = json.dumps(doc)[:1500]
        return [(text, f"project:{rel}")]

    def search(self, query: str, k: int = 20) -> list:
        self.refresh()
        q = _fts_query(query)
        if not q:
            return []
        rows = self.db.execute("SELECT text, source, bm25(p) AS s FROM p WHERE p MATCH ? ORDER BY s LIMIT ?",
                               (q, k)).fetchall()
        return [{"source": src, "text": text, "score": -s} for text, src, s in rows]


# --------------------------------------------------------------------------- embeddings (optional)


class OllamaEmbedder:
    """texts -> vectors through Ollama /api/embed (e.g. model='nomic-embed-text' or 'embeddinggemma')."""

    def __init__(self, model: str, host: str = "http://127.0.0.1:11434", timeout: int = 120):
        self.model, self.host, self.timeout = model, host.rstrip("/"), timeout

    def __call__(self, texts: list) -> list:
        req = urllib.request.Request(self.host + "/api/embed",
                                     data=json.dumps({"model": self.model, "input": texts}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())["embeddings"]


def _cos(a, b) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


# --------------------------------------------------------------------------- hybrid retriever


class HybridRetriever:
    """Merge candidates from several stores, rerank, return the top k as [S1]..[Sn] passages.

    sources: list of (name, store, weight); a store is anything with .search(query, k) -> [dict]
             whose dicts carry "source" and "text" (and optionally "score", "date").
    embed:   optional texts -> vectors callable; when set, semantic similarity replaces term coverage.
    """

    def __init__(self, sources: list, pool: int = 20, project_slots: int = 2,
                 half_life_days: float = 540.0, embed: Optional[Callable[[list], list]] = None,
                 today: Optional[date] = None):
        self.sources = [s for s in sources if s and s[1] is not None]
        self.pool = pool
        self.project_slots = project_slots
        self.half_life = half_life_days
        self.embed = embed
        self.today = today

    def candidates(self, query: str) -> list:
        out = []
        for name, store, weight in self.sources:
            try:
                hits = list(store.search(query, k=self.pool) or [])
            except Exception:
                continue  # a broken source must not take retrieval down
            scores = [float(h.get("score") or 0) for h in hits]
            lo, hi = (min(scores), max(scores)) if scores else (0, 0)
            for rank, h in enumerate(hits):
                s = float(h.get("score") or 0)
                norm = (s - lo) / (hi - lo) if hi > lo else 1.0 - rank / max(len(hits), 1)
                out.append({**h, "_src": name, "_w": weight, "_norm": norm})
        return out

    def _fresh(self, d) -> float:
        dt = _parse_date(d)
        if not dt:
            return 0.0
        age = max(((self.today or date.today()) - dt).days, 0)
        return 0.5 ** (age / self.half_life)

    def search(self, query: str, k: int = 6) -> list:
        cands = self.candidates(query)
        if not cands:
            return []
        qt = terms(query)
        if self.embed:
            try:
                vecs = self.embed([query] + [c["text"][:2000] for c in cands])
                sims = [_cos(vecs[0], v) for v in vecs[1:]]
            except Exception:
                sims = None
        else:
            sims = None
        for i, c in enumerate(cands):
            body = (str(c.get("title") or "") + " " + c["text"]).lower()
            coverage = sum(1 for t in qt if t in body) / len(qt) if qt else 0.0
            rel = sims[i] if sims is not None else coverage
            c["_rank"] = c["_w"] * (0.45 * c["_norm"] + 0.45 * rel + 0.10 * self._fresh(c.get("date")))

        seen, ranked = set(), []
        for c in sorted(cands, key=lambda c: c["_rank"], reverse=True):
            key = (c["source"], re.sub(r"\s+", " ", c["text"][:200]).lower())
            if key in seen:
                continue
            seen.add(key)
            ranked.append(c)

        proj = [c for c in ranked if str(c["source"]).startswith("project:")][: self.project_slots]
        rest = [c for c in ranked if c not in proj]
        picked = (proj + rest)[:k] if proj else rest[:k]
        picked.sort(key=lambda c: c["_rank"], reverse=True)
        return [{"id": f"S{i}", "source": c["source"], "text": c["text"], "date": c.get("date") or "",
                 "score": round(c["_rank"], 4)} for i, c in enumerate(picked, 1)]
