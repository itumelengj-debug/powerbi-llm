r"""
powerbi_agent.py — the final unification of Agent Jo's Power BI stack.

Three layers, now in one call:
  - KNOWLEDGE  -> the app's own retrieval index (agent.rag.DocumentStore):
                  the 1,326-doc Microsoft Power BI / Fabric corpus.
  - BEHAVIOUR  -> the `powerbi` Ollama model's baked-in rulebook (the brain):
                  scope lock, DAX / M / PBIR standards, safety gates.
  - EXECUTION  -> jo_powerbi.PowerBIAgent's gated tools (read_project,
                  write_report_files, stage_tmdl, run_powershell, lookup_docs)
                  with approval + audit + artifact verification.

The merge: retrieved passages are passed into pbi.run(question, context=[...])
as [S1]/[S2]-tagged source-of-truth, so the model answers product facts from
the index (citing them) and marks anything uncovered "Unverified:" instead of
guessing from memory. This is the same grounding discipline that made
gemma4:26b reliable, now in front of the Power BI rules and the tools.

Usage (CLI):
    python powerbi_agent.py "Why does my YTD measure show blanks in December?"
    python powerbi_agent.py --k 8 "Write a DAX measure for share of total notional"

Usage (library):
    from powerbi_agent import run_powerbi, retrieve_context
    res = run_powerbi(question, k=6, allowed_roots=[r"C:/PBI"])
    print(res.text)
    # res.issues    -> non-empty = verification flagged something
    # res.needs_user -> steps only the user can do (e.g. apply a staged TMDL script)
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from agent.rag import DocumentStore
from agent import config

# The adapter is the Claude-delivered Power BI route (standard library only).
try:
    from jo_powerbi import PowerBIAgent, PowerBIConfig, is_powerbi_request
except ImportError:  # allow running from a checkout that keeps it under agent-jo/
    sys.path.insert(0, r"C:\Tools\powerbi-llm\agent-jo")
    from jo_powerbi import PowerBIAgent, PowerBIConfig, is_powerbi_request


DEFAULT_MODEL = "powerbi"
DEFAULT_K = 6
OLLAMA_HOST = config.OLLAMA_HOST.rstrip("/")


def _safe_print(text):
    """Print without crashing the Windows cp1252 console on unicode."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def retrieve_context(question: str, k: int = DEFAULT_K) -> list[dict]:
    """Retrieve top-k chunks from Jo's Power BI index and shape them for the
    adapter's grounding block ([S1]/[S2]-tagged source-of-truth passages)."""
    store = DocumentStore()
    chunks = store.search(question, k=k)
    return [
        {"id": f"S{i}", "source": c["source"], "text": c["text"]}
        for i, c in enumerate(chunks, 1)
    ]


def run_powerbi(
    question: str,
    k: int = DEFAULT_K,
    allowed_roots: Optional[list] = None,
    approve: Optional[Callable[[dict], bool]] = None,
    audit: Optional[Callable[[dict], None]] = None,
    web_search: Optional[Callable[[str], list]] = None,
    model: str = DEFAULT_MODEL,
    think: Optional[bool] = None,
) -> "PowerBIResult":
    """Ground a Power BI request in the retrieval index and hand it to the
    powerbi model + gated tools.

    approve defaults to fail-closed (every write/script denied) — pass a
    callback that returns True to actually let the model write report files or
    run PowerShell. web_search enables lookup_docs (learn.microsoft.com only).
    """
    context = retrieve_context(question, k=k)
    cfg = PowerBIConfig(
        model=model,
        ollama_url=OLLAMA_HOST,
        allowed_roots=list(allowed_roots or []),
    )
    pbi = PowerBIAgent(cfg, approve=approve, audit=audit, web_search=web_search)
    return pbi.run(question, context=context, think=think)


def main():
    args = sys.argv[1:]
    k = DEFAULT_K
    model = DEFAULT_MODEL
    parts = []
    i = 0
    while i < len(args):
        if args[i] == "--k" and i + 1 < len(args):
            k = int(args[i + 1])
            i += 2
        elif args[i] == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        else:
            parts.append(args[i])
            i += 1
    question = " ".join(parts).strip()
    if not question:
        _safe_print("Usage: python powerbi_agent.py [--k N] [--model M] \"your question\"")
        return

    context = retrieve_context(question, k=k)
    _safe_print(f"[grounding: {len(context)} passages from the Power BI index]")
    for c in context:
        _safe_print(f"  [{c['id']}] {c['source']}")

    cfg = PowerBIConfig(model=model, ollama_url=OLLAMA_HOST, allowed_roots=[])
    pbi = PowerBIAgent(cfg)  # fail-closed: writes/scripts denied in CLI mode
    res = pbi.run(question, context=context)

    _safe_print("\n" + "=" * 72)
    _safe_print(res.text.strip())
    _safe_print("=" * 72)

    if res.issues:
        _safe_print(f"\n[VERIFY] {len(res.issues)} issue(s) flagged:")
        for iss in res.issues:
            _safe_print(f"  - {iss}")
    if res.needs_user:
        _safe_print("\n[NEEDS USER]")
        for n in res.needs_user:
            _safe_print(f"  - {n}")
    if res.thinking:
        _safe_print("\n[THINKING]\n" + res.thinking.strip())


if __name__ == "__main__":
    main()
