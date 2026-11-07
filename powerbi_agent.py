r"""
powerbi_agent.py - the unified Power BI stack in one call.

Three layers:
  - KNOWLEDGE  -> pbi_retrieval.HybridRetriever over
                    * Agent Jo's DocumentStore (agent.rag) when that package is importable,
                    * the local Microsoft Learn index kept current by refresh_docs.py,
                    * the user's own PBIP project (real table / column / measure names),
                    * curated design and modelling guides shipped in knowledge/,
                  reranked and passed in as [S1]..[Sn] source-of-truth passages.
  - BEHAVIOUR  -> the `powerbi` Ollama model's rulebook (the brain), full or modular.
  - EXECUTION  -> jo_powerbi.PowerBIAgent's gated tools (read_project, write_report_files,
                  stage_tmdl, run_powershell, lookup_docs, and run_dax_query when enabled),
                  approval + audit + artifact verification (incl. citation and PBIR schema checks).

Usage (CLI):
    python powerbi_agent.py "Why does my YTD measure show blanks in December?"
    python powerbi_agent.py --project C:\PBI\FX --dax "Write a YoY % measure and test it"
    python powerbi_agent.py --approve ask --project C:\PBI\FX "Add a Client Drilldown page"
    python powerbi_agent.py --search-only "incremental refresh polling"

Usage (library):
    from powerbi_agent import run_powerbi, retrieve_context
    res = run_powerbi(question, k=6, allowed_roots=[r"C:\PBI"], on_token=print_token)
    res.text / res.issues / res.needs_user
"""
from __future__ import annotations

import argparse
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "agent-jo"))     # jo_powerbi, pbi_dax, pbir_schema live there

from jo_powerbi import PowerBIAgent, PowerBIConfig, PowerBIResult, is_powerbi_request  # noqa: E402,F401
from pbi_retrieval import (DEFAULT_DOCS_DB, DocsIndex, GuideIndex, HybridRetriever, OllamaEmbedder,  # noqa: E402
                           ProjectIndex)

try:  # Agent Jo's own index and config, when this runs inside the Jo app
    from agent.rag import DocumentStore  # type: ignore
except ImportError:
    DocumentStore = None
try:
    from agent import config as _jo_config  # type: ignore
except ImportError:
    _jo_config = None

DEFAULT_MODEL = "powerbi"
DEFAULT_K = 6
OLLAMA_HOST = (os.environ.get("OLLAMA_HOST") or getattr(_jo_config, "OLLAMA_HOST", "")
               or "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA_HOST.startswith("http"):
    OLLAMA_HOST = "http://" + OLLAMA_HOST
DOCS_DB = os.environ.get("PBI_DOCS_DB") or str(DEFAULT_DOCS_DB)
EMBED_MODEL = os.environ.get("PBI_EMBED_MODEL", "")    # e.g. nomic-embed-text: enables embedding rerank


def _safe_print(text, end="\n", flush=False):
    """Print without crashing the Windows cp1252 console on unicode."""
    try:
        print(text, end=end, flush=flush)
    except UnicodeEncodeError:
        print(str(text).encode("ascii", "replace").decode("ascii"), end=end, flush=flush)


@lru_cache(maxsize=1)
def _jo_store():
    return DocumentStore() if DocumentStore else None


@lru_cache(maxsize=1)
def _docs_index():
    return DocsIndex(DOCS_DB) if Path(DOCS_DB).exists() else None


@lru_cache(maxsize=1)
def _guide_index():
    folder = HERE / "knowledge"
    return GuideIndex(folder) if folder.exists() else None


@lru_cache(maxsize=8)
def _project_index(roots: tuple):
    return ProjectIndex(roots) if roots else None


def get_retriever(project_roots: Optional[list] = None, embed_model: str = EMBED_MODEL) -> HybridRetriever:
    """Build (cheaply - stores are cached) the retriever over every available source."""
    sources = [("jo", _jo_store(), 1.0), ("learn", _docs_index(), 1.0), ("guide", _guide_index(), 1.0),
               ("project", _project_index(tuple(project_roots or ())), 0.9)]
    embed = OllamaEmbedder(embed_model, OLLAMA_HOST) if embed_model else None
    return HybridRetriever(sources, embed=embed)


def retrieve_context(question: str, k: int = DEFAULT_K, project_roots: Optional[list] = None) -> list:
    """Top-k passages for the adapter's grounding block ([S1]/[S2]-tagged source-of-truth)."""
    return get_retriever(project_roots).search(question, k=k)


def make_schema_validator(allow_fetch: bool = False):
    try:
        from pbir_schema import SchemaValidator
    except ImportError:
        return None
    v = SchemaValidator(allow_fetch=allow_fetch)
    return v if v.available else None


def make_dax_runner():
    from pbi_dax import DesktopDaxRunner
    return DesktopDaxRunner()


def ask_approval(action: dict) -> bool:
    """Interactive approval for the CLI: show exactly what would happen, default No."""
    _safe_print("\n" + "-" * 72)
    _safe_print(f"APPROVAL NEEDED: {action.get('tool')}")
    _safe_print(action.get("summary", ""))
    if action.get("script"):
        _safe_print("\n" + action["script"])
    _safe_print("-" * 72)
    try:
        return input("Approve? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def run_powerbi(
    question: str,
    k: int = DEFAULT_K,
    allowed_roots: Optional[list] = None,
    approve: Optional[Callable[[dict], bool]] = None,
    audit: Optional[Callable[[dict], None]] = None,
    web_search: Optional[Callable[[str], list]] = None,
    model: str = DEFAULT_MODEL,
    think: Optional[bool] = None,
    on_token: Optional[Callable[[str, str], None]] = None,
    dax_runner: Optional[Callable] = None,
    schema_validator: Optional[Callable] = None,
    brain_mode: str = "full",
    jo_system_prompt: str = "",
    images: Optional[list] = None,
    history: Optional[list] = None,
    context: Optional[list] = None,
) -> PowerBIResult:
    """Ground a Power BI request in retrieval and hand it to the powerbi model + gated tools.

    approve defaults to fail-closed (every write/script denied). allowed_roots are the folders the
    tools may touch; they are also indexed for retrieval so real names reach the model.
    context: pass your own passages to skip retrieval."""
    roots = list(allowed_roots or [])
    if context is None:
        context = retrieve_context(question, k=k, project_roots=roots)
    cfg = PowerBIConfig(model=model, ollama_url=OLLAMA_HOST, allowed_roots=roots, brain_mode=brain_mode)
    pbi = PowerBIAgent(cfg, approve=approve, audit=audit, web_search=web_search, dax_runner=dax_runner,
                       schema_validator=schema_validator, on_token=on_token)
    return pbi.run(question, jo_system_prompt=jo_system_prompt, images=images, history=history,
                   think=think, context=context)


def _stream_printer():
    state = {"kind": None}

    def on_token(kind, text):
        if kind == "thinking":
            if state["kind"] != "thinking":
                _safe_print("\n[thinking] ", end="")
        elif kind == "content" and state["kind"] == "thinking":
            _safe_print("\n[answer]\n", end="")
        state["kind"] = kind
        _safe_print(text, end="", flush=True)
    return on_token


def main(argv=None):
    ap = argparse.ArgumentParser(description="Grounded Power BI assistant (retrieval + powerbi brain + gated tools)")
    ap.add_argument("question", nargs="*")
    ap.add_argument("--k", type=int, default=DEFAULT_K)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--project", action="append", default=[], help="PBIP folder the tools may read/write (repeatable)")
    ap.add_argument("--approve", choices=["deny", "ask"], default="deny",
                    help="deny (default): every write and script is refused; ask: prompt for each")
    ap.add_argument("--dax", action="store_true", help="enable run_dax_query against the open Desktop model")
    ap.add_argument("--schema-check", choices=["off", "cache", "fetch"], default="cache",
                    help="PBIR $schema validation (needs jsonschema): cached schemas only, or fetch missing ones")
    ap.add_argument("--brain", choices=["full", "modular"], default="full")
    ap.add_argument("--think", dest="think", action="store_true", default=None)
    ap.add_argument("--no-think", dest="think", action="store_false")
    ap.add_argument("--no-stream", action="store_true")
    ap.add_argument("--search-only", action="store_true", help="show the retrieved passages and stop")
    a = ap.parse_args(argv)

    question = " ".join(a.question).strip()
    if not question:
        ap.print_usage()
        return 2

    context = retrieve_context(question, k=a.k, project_roots=a.project)
    _safe_print(f"[grounding: {len(context)} passages]")
    for c in context:
        _safe_print(f"  [{c['id']}] {c['source']}" + (f"  ({c['date']})" if c.get("date") else ""))
    if a.search_only:
        for c in context:
            _safe_print(f"\n--- [{c['id']}] {c['source']}\n{c['text']}")
        return 0

    schema = None if a.schema_check == "off" else make_schema_validator(a.schema_check == "fetch")
    on_token = None if a.no_stream else _stream_printer()
    res = run_powerbi(question, k=a.k, allowed_roots=a.project, model=a.model, think=a.think, context=context,
                      approve=ask_approval if a.approve == "ask" else None, on_token=on_token,
                      dax_runner=make_dax_runner() if a.dax else None, schema_validator=schema,
                      brain_mode=a.brain)

    if a.no_stream:
        _safe_print("\n" + "=" * 72)
        _safe_print(res.text.strip())
        _safe_print("=" * 72)
        if res.thinking:
            _safe_print("\n[THINKING]\n" + res.thinking.strip())
    else:
        _safe_print("\n" + "=" * 72)
    if res.issues:
        _safe_print(f"\n[VERIFY] {len(res.issues)} issue(s) flagged:")
        for iss in res.issues:
            _safe_print(f"  - {iss}")
    if res.needs_user:
        _safe_print("\n[NEEDS USER]")
        for n in res.needs_user:
            _safe_print(f"  - {n}")
    return 1 if res.issues else 0


if __name__ == "__main__":
    sys.exit(main())
