"""MCP server: JSON-RPC handling in-process and one real stdio session."""
import json
import os
import subprocess
import sys

import pytest

import mcp_server as ms
import powerbi_agent as pa
from conftest import ROOT, final


@pytest.fixture
def server(llm, sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr(pa, "OLLAMA_HOST", llm.url)
    monkeypatch.setattr(pa, "DOCS_DB", str(tmp_path / "none.sqlite"))
    pa._docs_index.cache_clear()
    pa._project_index.cache_clear()
    s = ms.Server([str(sandbox.parent)])
    s.schema = None
    return s


def rpc(s, method, params=None, mid=1):
    return s.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def text_of(reply):
    return reply["result"]["content"][0]["text"]


def test_initialize_and_list(server):
    r = rpc(server, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}})
    assert r["result"]["protocolVersion"] == "2025-03-26" and "tools" in r["result"]["capabilities"]
    assert rpc(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"
    names = [t["name"] for t in rpc(server, "tools/list")["result"]["tools"]]
    assert names == ["powerbi_ask", "powerbi_search_docs", "powerbi_read_project", "powerbi_verify"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert rpc(server, "ping")["result"] == {}
    assert rpc(server, "nope")["error"]["code"] == -32601


def test_optional_tools():
    s = ms.Server([], expose_writes=True)
    s.dax_runner = lambda q, n: {"results": []}
    names = [t["name"] for t in s.tools()]
    assert "powerbi_run_dax" in names and "powerbi_write_report_files" in names


def test_verify_tool(server):
    r = rpc(server, "tools/call", {"name": "powerbi_verify", "arguments": {"text": "```dax\nX = 1\n```"}})
    assert "missing '### <CHANNEL>' header" in text_of(r) and r["result"]["isError"] is False
    r = rpc(server, "tools/call", {"name": "powerbi_verify", "arguments": {"text": "### DAX\n```dax\nX = 1\n```"}})
    assert text_of(r).startswith("PASS")


def test_read_project_confined(server, sandbox):
    ok = rpc(server, "tools/call", {"name": "powerbi_read_project", "arguments": {"project_root": str(sandbox)}})
    assert "Client Name" in text_of(ok)
    bad = rpc(server, "tools/call", {"name": "powerbi_read_project", "arguments": {"project_root": "/etc"}})
    assert bad["result"]["isError"] is True and "outside" in text_of(bad)


def test_search_and_ask(server, llm):
    r = rpc(server, "tools/call", {"name": "powerbi_search_docs", "arguments": {"query": "notional measure", "k": 3}})
    hits = json.loads(text_of(r))
    assert hits and hits[0]["source"].startswith("project:")
    llm.script = [final("### DAX\n```dax\nX = 1\n```")]
    r = rpc(server, "tools/call", {"name": "powerbi_ask", "arguments": {"question": "Measure X"}})
    assert "X = 1" in text_of(r) and r["result"]["isError"] is False


def test_unknown_tool_and_missing_args(server):
    assert rpc(server, "tools/call", {"name": "rm_rf", "arguments": {}})["error"]["code"] == -32602
    r = rpc(server, "tools/call", {"name": "powerbi_verify", "arguments": {}})
    assert r["result"]["isError"] is True


def test_stdio_session(llm, tmp_path):
    env = {**os.environ, "OLLAMA_HOST": llm.url, "PBI_DOCS_DB": str(tmp_path / "none.sqlite")}
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            "not json"]
    stdin = "\n".join(m if isinstance(m, str) else json.dumps(m) for m in msgs) + "\n"
    p = subprocess.run([sys.executable, str(ROOT / "mcp_server.py")], input=stdin, capture_output=True, text=True,
                       env=env, timeout=60)
    replies = [json.loads(ln) for ln in p.stdout.splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[0]["result"]["serverInfo"]["name"] == "powerbi-llm"
    assert replies[2]["error"]["code"] == -32700
    assert "powerbi MCP server" in p.stderr
