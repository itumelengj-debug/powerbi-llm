"""
Tests for jo_powerbi.py against a mock Ollama server (no GPU needed).
Run:  python -m pytest -q test_jo_powerbi.py
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import jo_powerbi as jp

HEX_PAGE, HEX_VIS = "4b1d7e20c9a35f86d2e0", "9e27c4a1f06b3d85e7a2"
SCHEMA_V = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.9.0/schema.json"
SCHEMA_P = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.0.0/schema.json"

PAGE = {"$schema": SCHEMA_P, "name": HEX_PAGE, "displayName": "Client Drilldown",
        "displayOption": "FitToPage", "height": 720, "width": 1280}
CARD = {"$schema": SCHEMA_V, "name": HEX_VIS,
        "position": {"x": 24, "y": 112, "z": 1000, "height": 120, "width": 296, "tabOrder": 1000},
        "visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [{
            "field": {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}}, "Property": "Notional (ZAR)"}},
            "queryRef": "_Measures.Notional (ZAR)", "nativeQueryRef": "Notional (ZAR)"}]}}},
            "drillFilterOtherVisuals": True}}
GOOD_FINAL = "Done.\n### FILE: pages/x/visuals/y/visual.json\n```json\n" + json.dumps(CARD, indent=2) + "\n```\n"


# ------------------------------------------------------------------ mock Ollama
class Mock:
    def __init__(self):
        self.script, self.requests = [], []


def make_server(mock: Mock):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj):
            b = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            mock.requests.append((self.path, body))
            if self.path == "/api/show":
                return self._send({"system": "BRAIN: Power BI only.", "capabilities": ["completion", "tools", "thinking"]})
            if self.path == "/api/chat":
                return self._send({"message": mock.script.pop(0)})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def final(text):
    return {"role": "assistant", "content": text}


@pytest.fixture
def env(tmp_path):
    mock = Mock()
    srv = make_server(mock)
    proj = tmp_path / "PBI" / "FX"
    sm = proj / "FX.SemanticModel" / "definition" / "tables"
    sm.mkdir(parents=True)
    (sm / "_Measures.tmdl").write_text("table _Measures\n\n\tmeasure 'Notional (ZAR)' = SUM ( Trades[NotionalZAR] )\n"
                                       "\t\tformatString: #,##0\n", encoding="utf-8")
    (sm / "Client.tmdl").write_text("table Client\n\n\tcolumn 'Client Name'\n\t\tdataType: string\n", encoding="utf-8")
    pages = proj / "FX.Report" / "definition" / "pages"
    (pages / "aaaaaaaaaaaaaaaaaaaa").mkdir(parents=True)
    (pages / "pages.json").write_text(json.dumps({"pageOrder": ["aaaaaaaaaaaaaaaaaaaa"],
                                                  "activePageName": "aaaaaaaaaaaaaaaaaaaa"}), encoding="utf-8")
    (pages / "aaaaaaaaaaaaaaaaaaaa" / "page.json").write_text(json.dumps(
        {"name": "aaaaaaaaaaaaaaaaaaaa", "displayName": "Overview", "height": 720, "width": 1280}), encoding="utf-8")
    events, approvals = [], []
    cfg = jp.PowerBIConfig(ollama_url=f"http://127.0.0.1:{srv.server_port}", allowed_roots=[str(tmp_path / "PBI")],
                           backup_dir=str(tmp_path / "backups"), staging_dir=str(tmp_path / "staging"))
    yield {"mock": mock, "proj": proj, "cfg": cfg, "events": events, "approvals": approvals, "tmp": tmp_path}
    srv.shutdown()


def agent(env, approve=True, web=None):
    def gate(action):
        env["approvals"].append(action)
        return approve
    return jp.PowerBIAgent(env["cfg"], approve=gate, audit=env["events"].append, web_search=web)


# ------------------------------------------------------------------ tests
def test_router():
    assert jp.is_powerbi_request("Add a DAX measure for YTD notional")
    assert jp.is_powerbi_request("build a report page with a slicer")
    assert not jp.is_powerbi_request("what's the weather in Joburg")


def test_prompt_merge_and_request_shape(env):
    env["mock"].script = [final("### DAX\n```dax\nX = 1\n```")]
    agent(env).run("Measure X", jo_system_prompt="JO RULES: cite sources.")
    chat = [b for p, b in env["mock"].requests if p == "/api/chat"][0]
    sysmsg = chat["messages"][0]["content"]
    assert sysmsg.startswith("BRAIN: Power BI only.") and "JO RULES" in sysmsg and "## TOOLS" in sysmsg
    assert chat["tools"] and "options" not in chat and chat["think"] is False


def test_think_auto_on_for_reasoning(env):
    env["mock"].script = [final("### DAX\n```dax\nX = 1\n```")]
    agent(env).run("Why is my measure slow?")
    chat = [b for p, b in env["mock"].requests if p == "/api/chat"][0]
    assert chat["think"] is True


def test_full_page_build(env):
    report = str(env["proj"] / "FX.Report")
    env["mock"].script = [
        call("read_project", project_root=str(env["proj"])),
        call("write_report_files", report_path=report, files=[
            {"path": f"pages/{HEX_PAGE}/page.json", "content": json.dumps(PAGE)},
            {"path": f"pages\\{HEX_PAGE}\\visuals\\{HEX_VIS}\\visual.json", "content": json.dumps(CARD)}]),
        final(GOOD_FINAL)]
    res = agent(env).run("Add a Client Drilldown page with a notional card")
    assert res.issues == []
    assert [a["status"] for a in res.actions] == ["ok", "ok"]
    inv = [m for p, b in env["mock"].requests if p == "/api/chat" for m in b["messages"] if m["role"] == "tool"][0]
    assert "Notional (ZAR)" in inv["content"] and "Client Name" in inv["content"]
    d = env["proj"] / "FX.Report" / "definition" / "pages"
    assert (d / HEX_PAGE / "visuals" / HEX_VIS / "visual.json").exists()
    assert json.loads((d / "pages.json").read_text())["pageOrder"] == ["aaaaaaaaaaaaaaaaaaaa", HEX_PAGE]
    assert list((env["tmp"] / "backups").iterdir())
    assert any(e["event"] == "pbi_write" for e in env["events"])
    assert env["approvals"][0]["tool"] == "write_report_files"


def test_write_denied_changes_nothing(env):
    report = str(env["proj"] / "FX.Report")
    env["mock"].script = [
        call("write_report_files", report_path=report,
             files=[{"path": f"pages/{HEX_PAGE}/page.json", "content": json.dumps(PAGE)}]),
        final("Not written - approval declined.")]
    res = agent(env, approve=False).run("Add a page")
    assert res.actions[0]["status"] == "denied"
    assert not (env["proj"] / "FX.Report" / "definition" / "pages" / HEX_PAGE).exists()


def test_no_callback_fails_closed(env):
    a = jp.PowerBIAgent(env["cfg"], audit=env["events"].append)
    out = a.tool_run_powershell("Get-Date", "show date")
    assert out.startswith("DENIED")


@pytest.mark.parametrize("path,content,msg", [
    ("../../evil/page.json", json.dumps(PAGE), "not allowed"),
    ("report.json", "{}", "not allowed"),
    (f"pages/{HEX_PAGE}/page.json", "{bad json", "invalid JSON"),
    (f"pages/{HEX_PAGE}/visuals/wrongname/visual.json", json.dumps(CARD), "must equal its folder"),
    (f"pages/bbbbbbbbbbbbbbbbbbbb/visuals/{HEX_VIS}/visual.json", json.dumps(CARD), "does not exist"),
])
def test_write_validation(env, path, content, msg):
    a = agent(env)
    with pytest.raises(jp.ToolError, match=msg):
        a.tool_write_report_files(str(env["proj"] / "FX.Report"), [{"path": path, "content": content}])
    assert env["approvals"] == []          # rejected before asking the user


def test_outside_allowed_roots(env, tmp_path):
    with pytest.raises(jp.ToolError, match="outside"):
        agent(env).tool_read_project(str(tmp_path.parent))


def test_stage_tmdl(env):
    a = agent(env)
    ok = a.tool_stage_tmdl("createOrReplace\n\n\tref table _Measures\n\n\t\tmeasure 'N' = 1\n", "N measure")
    assert ok.startswith("OK") and list((env["tmp"] / "staging").glob("*.tmdl"))
    with pytest.raises(jp.ToolError, match="ref table"):
        a.tool_stage_tmdl("createOrReplace\n\n\ttable _Measures\n", "bad")
    with pytest.raises(jp.ToolError, match="tabs"):
        a.tool_stage_tmdl("createOrReplace\n\n    ref table _Measures\n", "bad")


def test_outbound_scrub(env):
    seen = []

    def web(q):
        seen.append(q)
        return [{"title": "PBIR", "url": "https://learn.microsoft.com/power-bi/x", "snippet": "."},
                {"title": "blog", "url": "https://random.example/x", "snippet": "."}]
    a = agent(env, web=web)
    a.tool_read_project(str(env["proj"]))
    assert a.tool_lookup_docs("Notional (ZAR) slicer formatting").startswith("ERROR")
    assert a.tool_lookup_docs("client 12345678 refresh").startswith("ERROR")
    out = json.loads(a.tool_lookup_docs("PBIR slicer dropdown mode"))
    assert len(out) == 1 and "learn.microsoft.com" in out[0]["url"] and seen == ["PBIR slicer dropdown mode"]


def test_fix_round(env):
    bad = "### FILE: v.json\n```json\n{\"name\": \"" + HEX_VIS + "\", \"position\": {}, \"visual\": {\"visualType\": \"card\"}}\n```"
    env["mock"].script = [final(bad), final(GOOD_FINAL)]
    res = agent(env).run("card for notional")
    assert res.issues == []
    last = [b for p, b in env["mock"].requests if p == "/api/chat"][-1]["messages"][-1]["content"]
    assert "verification failed" in last and "$schema" in last


@pytest.mark.parametrize("text,needle", [
    ("```dax\nX = 1\n```", "header"),
    ("### FILE: a\n```json\n{oops}\n```", "invalid JSON"),
    ("### TMDL VIEW\n```tmdl\ncreateOrReplace\n\n    ref table T\n```", "tabs"),
    ("### TMDL VIEW\n```tmdl\ncreateOrReplace\n\n\ttable T\n```", "ref table"),
    ("### POWERSHELL\n```powershell\n$x = @\"\n{\"$schema\": 1}\n\"@\n```", "double-quoted"),
    ("### POWERSHELL\n```powershell\n$p = 'definition'\n[IO.File]::WriteAllText($p, 'x')\n```", "Desktop"),
    ("### POWERSHELL\n```powershell\n$password = 'hunter22'\n```", "secret"),
    ("### DAX\n```dax\nX = CALCULATE( ... )\n```", "placeholder"),
])
def test_verifier_catches(text, needle):
    assert any(needle.lower() in i.lower() for i in jp.verify_artifacts(text)), jp.verify_artifacts(text)


def test_verifier_passes_good():
    assert jp.verify_artifacts(GOOD_FINAL) == []


def test_images_encoded(env):
    env["mock"].script = [final("Out of scope - I only do Power BI.")]
    agent(env).run("what is this visual", images=[b"\x89PNG fake"])
    chat = [b for p, b in env["mock"].requests if p == "/api/chat"][0]
    assert chat["messages"][-1]["images"][0] == "iVBORyBmYWtl"


# ------------------------------------------------------------------ llama.cpp backend + RAG context
def make_openai_server(mock: Mock):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            mock.requests.append((self.path, body))
            msg = mock.script.pop(0)
            out = {"choices": [{"message": msg}]}
            b = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def oai_call(name, **args):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_llamacpp_backend_tools_and_images(env, tmp_path):
    mock = Mock()
    srv = make_openai_server(mock)
    brain = tmp_path / "brain.txt"
    brain.write_text("BRAIN: Power BI only.", encoding="utf-8")
    cfg = env["cfg"]
    cfg.backend = "llamacpp"
    cfg.llamacpp_url = f"http://127.0.0.1:{srv.server_port}"
    cfg.system_prompt_path = str(brain)
    mock.script = [oai_call("read_project", project_root=str(env["proj"])), {"role": "assistant", "content": GOOD_FINAL}]
    res = agent(env).run("inventory the model", images=[b"\x89PNG fake"])
    srv.shutdown()

    first = mock.requests[0][1]
    assert mock.requests[0][0] == "/v1/chat/completions"
    assert first["messages"][0]["content"].startswith("BRAIN: Power BI only.")
    assert "think" not in first and first["tools"]
    assert first["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
    tool_msg = [m for m in mock.requests[1][1]["messages"] if m["role"] == "tool"][0]
    assert tool_msg["tool_call_id"] == "call_1" and "Notional (ZAR)" in tool_msg["content"]
    assert res.issues == [] and res.actions[0]["status"] == "ok"


def test_rag_context_block(env):
    env["mock"].script = [final("### DAX\n```dax\nX = 1 // [S1]\n```")]
    agent(env).run("does PBIR support X", context=[
        {"id": "S1", "source": "learn.microsoft.com/power-bi/a", "text": "PBIR stores pages in folders."}])
    sysmsg = [b for p, b in env["mock"].requests if p == "/api/chat"][0]["messages"][0]["content"]
    assert "RETRIEVED CONTEXT" in sysmsg and "[S1] learn.microsoft.com/power-bi/a" in sysmsg
    assert "Unverified:" in sysmsg and "PBIR stores pages in folders." in sysmsg


def test_brain_extraction_from_modelfile():
    brain = jp.brain_from_modelfile(str(Path(__file__).resolve().parent.parent / "Modelfile"))
    assert brain.startswith("You are POWERBI") and "PBIP / PBIR ENGINEERING" in brain
