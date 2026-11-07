"""Dashboard Studio wired into the agent, the verifier, the MCP server, retrieval and the CLI."""
import json
import re

import pytest

import jo_powerbi as jp
import mcp_server as ms
import powerbi_agent as pa
from conftest import call, final

SPEC = {"title": "Desk Overview",
        "kpis": ["[Notional (ZAR)]", {"measure": "[USDZAR Rate]", "label": "Avg USDZAR"}],
        "visuals": [{"type": "line", "title": "Notional by month", "category": "'Date'[Month]", "values": "[Notional (ZAR)]"},
                    {"type": "bar", "title": "Top clients", "category": "'Client'[Client Name]", "values": "[Notional (ZAR)]"}],
        "slicers": ["'Date'[Year]"]}


def make_agent(llm, tmp_path, sandbox, approve=True, **cfg):
    approvals, events = [], []

    def gate(action):
        approvals.append(action)
        return approve
    c = jp.PowerBIConfig(ollama_url=llm.url, allowed_roots=[str(sandbox.parent)], backup_dir=str(tmp_path / "bk"),
                         staging_dir=str(tmp_path / "st"), **cfg)
    return jp.PowerBIAgent(c, approve=gate, audit=events.append), approvals, events


def page_dirs(sandbox):
    return sorted(p.name for p in (sandbox / "FX.Report" / "definition" / "pages").iterdir() if p.is_dir())


def test_design_tools_offered_and_switchable(llm, tmp_path, sandbox):
    llm.script = [final("ok"), final("ok")]
    make_agent(llm, tmp_path, sandbox)[0].run("hi")
    make_agent(llm, tmp_path, sandbox, design_tools=False)[0].run("hi")
    on, off = [[t["function"]["name"] for t in b["tools"]] for p, b in llm.requests if p == "/api/chat"]
    assert {"design_dashboard", "make_theme", "lint_design", "svg_measure"} <= set(on)
    assert not {"design_dashboard", "make_theme"} & set(off)
    sysmsg = [b for p, b in llm.requests if p == "/api/chat"][0]["messages"][0]["content"]
    assert "call design_dashboard with a spec" in sysmsg


def test_design_dashboard_writes_page_after_approval(llm, tmp_path, sandbox):
    report = sandbox / "FX.Report"
    llm.script = [call("design_dashboard", spec=SPEC, report_path=str(report)), final("Built the page.")]
    agent, approvals, events = make_agent(llm, tmp_path, sandbox)
    res = agent.run("Build a desk overview page")
    assert res.actions[0]["status"] == "ok" and approvals[0]["tool"] == "write_report_files"
    pages = json.loads((report / "definition" / "pages" / "pages.json").read_text(encoding="utf-8"))["pageOrder"]
    assert len(pages) == 2
    new = report / "definition" / "pages" / pages[1]
    assert json.loads((new / "page.json").read_text(encoding="utf-8"))["displayName"] == "Desk Overview"
    assert len(list(new.glob("visuals/*/visual.json"))) == 6
    d = res.design[0]
    assert d["tool"] == "design_dashboard" and d["score"] >= 90 and d["preview"].endswith(".html")
    assert (tmp_path / "st" / "previews").exists() and open(d["preview"], encoding="utf-8").read().startswith("<!doctype html>")
    tool_msg = [m for p, b in llm.requests if p == "/api/chat" for m in b["messages"] if m["role"] == "tool"][0]
    assert "Design score" in tool_msg["content"] and "OK: wrote 7 file(s)" in tool_msg["content"]
    assert any(e["event"] == "pbi_design" for e in events)


def test_design_dashboard_denied_writes_nothing(llm, tmp_path, sandbox):
    before = page_dirs(sandbox)
    llm.script = [call("design_dashboard", spec=SPEC, report_path=str(sandbox / "FX.Report")), final("Denied.")]
    res = make_agent(llm, tmp_path, sandbox, approve=False)[0].run("Build it")
    assert res.actions[0]["status"] == "denied" and page_dirs(sandbox) == before
    assert res.design and res.design[0]["preview"]                 # the preview is still there to look at


def test_design_dashboard_stages_without_report(llm, tmp_path, sandbox):
    llm.script = [call("design_dashboard", spec=json.dumps(SPEC)), final("Staged.")]
    res = make_agent(llm, tmp_path, sandbox)[0].run("Design a page")
    assert res.actions[0]["status"] == "ok"
    step = [n for n in res.needs_user if "write-page.ps1" in n][0]
    writer = re.search(r"run (\S+write-page\.ps1)", step).group(1)
    script = open(writer, encoding="utf-8-sig").read()
    assert "param([Parameter(Mandatory = $true)][string]$Report)" in script
    assert "PBIDesktop" in script and "Copy-Item $Report $backup" in script
    blocks = re.findall(r"@'\n(.*?)\n'@", script, re.S)
    assert len(blocks) == 7 and all(json.loads(b) for b in blocks)
    assert jp.verify_artifacts("### POWERSHELL\n```powershell\n" + script + "```") == []


def test_design_dashboard_bad_fields_are_reported(llm, tmp_path, sandbox):
    spec = dict(SPEC, kpis=["[Notional ZAR]"])
    llm.script = [call("design_dashboard", spec=spec, report_path=str(sandbox / "FX.Report")), final("Fixing.")]
    res = make_agent(llm, tmp_path, sandbox)[0].run("Build it")
    assert res.actions[0]["status"] == "error"
    assert "did you mean _Measures[Notional (ZAR)]" in res.actions[0]["detail"]


def test_make_theme_stages_and_restyles_session(llm, tmp_path, sandbox):
    agent = make_agent(llm, tmp_path, sandbox)[0]
    out = agent.tool_make_theme(brand_color="#C8102E", name="Desk Red")
    path = re.search(r"staged at (\S+\.json)", out).group(1)
    theme = json.load(open(path, encoding="utf-8"))
    assert theme["name"] == "Desk Red" and theme["dataColors"][0] == "#C8102E"
    assert "USER STEP: in Power BI Desktop, View > Themes" in out and "Accessibility: passes" in out
    assert agent._tokens().accent == "#C8102E"
    with pytest.raises(jp.ToolError):
        agent.tool_make_theme(preset="neon")


def test_lint_design_tool(llm, tmp_path, sandbox):
    llm.script = [call("lint_design", report_path=str(sandbox / "FX.Report")), final("Here are the fixes.")]
    res = make_agent(llm, tmp_path, sandbox)[0].run("Review my report design")
    assert res.actions[0]["status"] == "ok" and res.design[0]["tool"] == "lint_design"
    tool_msg = [m for p, b in llm.requests if p == "/api/chat" for m in b["messages"] if m["role"] == "tool"][0]
    assert "Report design score" in tool_msg["content"] and "alt text" in tool_msg["content"]


def test_svg_measure_tool_stages_tmdl(llm, tmp_path, sandbox):
    agent = make_agent(llm, tmp_path, sandbox)[0]
    out = agent.tool_svg_measure("sparkline", "[Notional (ZAR)]", axis="'Date'[Date]")
    staged = re.search(r"staged '.*?' at (\S+\.tmdl)", out).group(1)
    script = open(staged, encoding="utf-8").read()
    assert script.startswith("createOrReplace") and "dataCategory: ImageUrl" in script and "%23" in script
    assert "EVALUATE ROW" in out
    with pytest.raises(jp.ToolError, match="needs axis"):
        agent.tool_svg_measure("sparkline", "[Notional (ZAR)]")
    out = agent.tool_svg_measure("colour_rule", "[Notional (ZAR)]")
    assert "hex colour" in out


def test_verifier_flags_overlapping_handwritten_visuals():
    def vis(name, x, y):
        return {"$schema": "x", "name": name, "position": {"x": x, "y": y, "width": 400, "height": 300},
                "visual": {"visualType": "lineChart", "query": {"queryState": {"Y": {"projections": [{
                    "field": {"Measure": {"Expression": {"SourceRef": {"Entity": "M"}}, "Property": "V"}},
                    "queryRef": "M.V", "nativeQueryRef": "V"}]}}}}}
    a, b, c = vis("aaaaaaaaaaaaaaaaaaaa", 24, 112), vis("bbbbbbbbbbbbbbbbbbbb", 200, 200), vis("cccccccccccccccccccc", 440, 112)
    text = "".join(f"### FILE: v{i}.json\n```json\n{json.dumps(d)}\n```\n" for i, d in enumerate([a, b]))
    assert any(i.startswith("layout:") and "overlap" in i for i in jp.verify_artifacts(text))
    ok = "".join(f"### FILE: v{i}.json\n```json\n{json.dumps(d)}\n```\n" for i, d in enumerate([a, c]))
    assert not any(i.startswith("layout:") for i in jp.verify_artifacts(ok))


def test_mcp_design_tools(llm, tmp_path, sandbox, monkeypatch):
    monkeypatch.setattr(pa, "OLLAMA_HOST", llm.url)
    monkeypatch.setenv("HOME", str(tmp_path))
    s = ms.Server([str(sandbox.parent)])
    s.schema = None
    report = str(sandbox / "FX.Report")

    def callt(name, **args):
        return s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})

    before = page_dirs(sandbox)
    r = callt("powerbi_design_dashboard", spec=SPEC, report_path=report)
    text = r["result"]["content"][0]["text"]
    assert r["result"]["isError"] is False and text.startswith("STAGED") and page_dirs(sandbox) == before
    s2 = ms.Server([str(sandbox.parent)], expose_writes=True)
    s2.schema = None
    r = s2.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "powerbi_design_dashboard", "arguments": {"spec": SPEC, "report_path": report}}})
    assert r["result"]["content"][0]["text"].startswith("OK: wrote") and len(page_dirs(sandbox)) == len(before) + 1
    r = callt("powerbi_design_dashboard", spec=dict(SPEC, kpis=["[Nope]"]))
    assert r["result"]["isError"] is True and "not in the model" in r["result"]["content"][0]["text"]
    r = callt("powerbi_lint_design", report_path=report)
    assert "Report design score" in r["result"]["content"][0]["text"]
    r = callt("powerbi_make_theme", preset="midnight")
    assert "theme 'Midnight'" in r["result"]["content"][0]["text"]


def test_guides_reach_retrieval(monkeypatch, tmp_path):
    monkeypatch.setattr(pa, "DOCS_DB", str(tmp_path / "none.sqlite"))
    pa._docs_index.cache_clear()
    ctx = pa.retrieve_context("how should KPI tiles show variance against prior year", k=4)
    assert ctx and ctx[0]["source"].startswith("guide:design/kpi-tiles.md")
    assert ctx[0]["date"] == "2026-11-06"


def test_studio_cli(tmp_path, capsys, sandbox):
    import dashboard_studio as ds
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SPEC))
    assert ds.main(["build", str(spec), "--report", str(sandbox / "FX.Report"), "--out", str(tmp_path / "out"),
                    "--theme", "midnight"]) == 0
    out = capsys.readouterr().out
    assert "Design score" in out and (tmp_path / "out" / "preview.html").exists()
    assert (tmp_path / "out" / "write-page.ps1").exists() and (tmp_path / "out" / "theme.json").exists()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(dict(SPEC, kpis=["[Nope]"])))
    assert ds.main(["build", str(bad), "--report", str(sandbox / "FX.Report"), "--out", str(tmp_path / "o2")]) == 2
    assert ds.main(["lint", str(sandbox / "FX.Report")]) == 0
    assert ds.main(["theme", "--preset", "okabe-ito", "-o", str(tmp_path / "t.json")]) == 0
    assert json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))["name"] == "Okabe-Ito"
    assert ds.main(["preview", str(sandbox / "FX.Report"), "-o", str(tmp_path / "p.svg")]) == 0
    assert (tmp_path / "p.svg").read_text(encoding="utf-8").startswith("<svg")
    assert ds.main(["gallery", "-o", str(tmp_path / "g.html"), "--preset", "mono"]) == 0
    assert (tmp_path / "g.html").read_text(encoding="utf-8").count("<svg") == len(ds.SAMPLE_SPECS)
    capsys.readouterr()
    assert ds.main(["svg-measure", "progress", "--measure", "[A]", "--target", "[B]"]) == 0
    assert "dataCategory: ImageUrl" in capsys.readouterr().out
    assert ds.main(["themes"]) == 0


def test_studio_review_sends_screenshot_to_model(llm, tmp_path, capsys):
    import base64
    import dashboard_studio as ds
    shot = tmp_path / "page.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    llm.default = final("Hierarchy 4/5: KPIs lead. Top fix: align the bar chart to the 16 px gutter.")
    assert ds.main(["review", str(shot), "--host", llm.url]) == 0
    assert "Top fix" in capsys.readouterr().out
    body = [b for p, b in llm.requests if p == "/api/chat"][0]
    user = body["messages"][-1]
    assert user["images"] == [base64.b64encode(shot.read_bytes()).decode()]
    assert "senior report designer" in user["content"] and body["think"] is True
