"""End-to-end: retrieval (docs + project) -> brain -> tools -> verifier, without the Jo package."""
import builtins

import pytest

import powerbi_agent as pa
from conftest import ROOT, call, final
from pbi_retrieval import DocsIndex


@pytest.fixture
def stack(llm, tmp_path, monkeypatch):
    db = tmp_path / "docs.sqlite"
    d = DocsIndex(db)
    d.upsert_page("https://learn.microsoft.com/en-us/dax/sameperiodlastyear-function-dax", "SAMEPERIODLASTYEAR",
                  "SAMEPERIODLASTYEAR returns a table that contains a column of dates shifted one year back in time "
                  "from the dates in the specified dates column, in the current context.", "2026-06-01")
    d.close()
    monkeypatch.setattr(pa, "DOCS_DB", str(db))
    monkeypatch.setattr(pa, "OLLAMA_HOST", llm.url)
    pa._docs_index.cache_clear()
    pa._project_index.cache_clear()
    yield llm
    pa._docs_index.cache_clear()
    pa._project_index.cache_clear()


def test_no_hardcoded_windows_path():
    code = (ROOT / "powerbi_agent.py").read_text()
    assert 'sys.path.insert(0, r"C:' not in code and "agent-jo" in code


def test_retrieve_context_merges_docs_and_project(stack, sandbox):
    ctx = pa.retrieve_context("year over year notional measure SAMEPERIODLASTYEAR", k=6, project_roots=[str(sandbox)])
    srcs = [c["source"] for c in ctx]
    assert any("sameperiodlastyear" in s for s in srcs)
    assert any(s.startswith("project:") for s in srcs)
    assert [c["id"] for c in ctx] == [f"S{i}" for i in range(1, len(ctx) + 1)]


def test_run_powerbi_grounded_end_to_end(stack, sandbox):
    stack.script = [call("read_project", project_root=str(sandbox)),
                    final("### DAX\n```dax\nNotional YoY % =\nVAR PY = CALCULATE ( [Notional (ZAR)], "
                          "SAMEPERIODLASTYEAR ( 'Date'[Date] ) )\nRETURN DIVIDE ( [Notional (ZAR)] - PY, PY )\n```\n"
                          "- Uses SAMEPERIODLASTYEAR [S1].")]
    tokens = []
    res = pa.run_powerbi("YoY % of notional with SAMEPERIODLASTYEAR", allowed_roots=[str(sandbox)],
                         on_token=lambda k, t: tokens.append(t))
    assert res.issues == [] and "[S1]" in res.text and tokens
    sysmsg = [b for p, b in stack.requests if p == "/api/chat"][0]["messages"][0]["content"]
    assert "RETRIEVED CONTEXT" in sysmsg and "shifted one year back" in sysmsg and "(updated 2026-06-01)" in sysmsg


def test_writes_fail_closed_by_default(stack, sandbox):
    page = "4b1d7e20c9a35f86d2e0"
    stack.script = [call("write_report_files", report_path=str(sandbox / "FX.Report"), files=[
        {"path": f"pages/{page}/page.json", "content": '{"name": "%s", "displayName": "X", "height": 720, '
                                                       '"width": 1280}' % page}]),
                    final("I could not write the page.")]
    res = pa.run_powerbi("add page X", allowed_roots=[str(sandbox)], context=[])
    assert res.actions[0]["status"] == "denied"
    assert not (sandbox / "FX.Report" / "definition" / "pages" / page).exists()


def test_cli_search_only(stack, sandbox, capsys):
    assert pa.main(["--search-only", "--project", str(sandbox), "SAMEPERIODLASTYEAR", "dates"]) == 0
    out = capsys.readouterr().out
    assert "[grounding:" in out and "shifted one year back" in out


def test_cli_answer_no_stream(stack, capsys):
    stack.script = [final("### DAX\n```dax\nX = 1\n```")]
    assert pa.main(["--no-stream", "Measure X"]) == 0
    assert "X = 1" in capsys.readouterr().out


def test_cli_returns_1_on_unfixed_issues(stack, capsys):
    stack.script = [final("```dax\nX = 1\n```"), final("```dax\nX = 1\n```")]
    assert pa.main(["--no-stream", "Measure X"]) == 1
    assert "[VERIFY]" in capsys.readouterr().out


def test_ask_approval(monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")
    assert pa.ask_approval({"tool": "run_powershell", "summary": "list workspaces", "script": "Get-PowerBIWorkspace"})
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")
    assert not pa.ask_approval({"tool": "write_report_files", "summary": "write 2 files"})
    assert "Get-PowerBIWorkspace" in capsys.readouterr().out


def test_rag_pbi_legacy_uses_shared_retriever(stack, capsys):
    import rag_pbi
    stack.script = [final("SAMEPERIODLASTYEAR shifts dates back one year [S1].")]
    assert rag_pbi.main(["SAMEPERIODLASTYEAR", "dates"]) == 0
    body = [b for p, b in stack.requests if p == "/api/chat"][0]
    assert body["model"] == "gemma4:26b" and "[S1]" in body["messages"][1]["content"]
