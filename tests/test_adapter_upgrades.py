"""Streaming, run_dax_query, citation checks, modular brain and the schema hook in jo_powerbi."""
import json

import jo_powerbi as jp
from conftest import call, final, real_brain

HEX_VIS = "9e27c4a1f06b3d85e7a2"
SCHEMA_V = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.9.0/schema.json"


def make_agent(llm, tmp_path, **kw):
    backend = kw.pop("backend", "ollama")
    cfg_kw = {k: kw.pop(k) for k in list(kw) if k in ("brain_mode", "system_prompt_path", "allowed_roots")}
    cfg = jp.PowerBIConfig(backend=backend, ollama_url=llm.url, llamacpp_url=llm.url,
                           backup_dir=str(tmp_path / "bk"), staging_dir=str(tmp_path / "st"), **cfg_kw)
    events = []
    agent = jp.PowerBIAgent(cfg, audit=events.append, **kw)
    return agent, events


# ------------------------------------------------------------------ streaming
def test_ollama_streaming_tokens_and_thinking(llm, tmp_path):
    llm.script = [final("### DAX\n```dax\nTotal = SUM ( Trades[NotionalZAR] )\n```", thinking="consider SUM")]
    got = []
    agent, _ = make_agent(llm, tmp_path, on_token=lambda k, t: got.append((k, t)))
    res = agent.run("Measure: total notional")
    body = [b for p, b in llm.requests if p == "/api/chat"][0]
    assert body["stream"] is True
    assert "".join(t for k, t in got if k == "content") == res.text
    assert "".join(t for k, t in got if k == "thinking") == "consider SUM" == res.thinking
    assert res.text.startswith("### DAX") and res.issues == []


def test_ollama_streaming_tool_call_round(llm, tmp_path, sandbox):
    llm.script = [call("read_project", project_root=str(sandbox)), final("Done.")]
    got = []
    agent, _ = make_agent(llm, tmp_path, allowed_roots=[str(sandbox.parent)], on_token=lambda k, t: got.append((k, t)))
    res = agent.run("What tables are in my model?")
    assert [a["status"] for a in res.actions] == ["ok"]
    assert ("status", "[tool] read_project\n") in got
    tool_msg = [m for p, b in llm.requests if p == "/api/chat" for m in b["messages"] if m["role"] == "tool"][0]
    assert "Client Name" in tool_msg["content"]


def test_llamacpp_streaming_reassembles_tool_call_fragments(llm, tmp_path, sandbox):
    brain = tmp_path / "brain.txt"
    brain.write_text("BRAIN", encoding="utf-8")
    llm.script = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "function": {"name": "read_project", "arguments": {"project_root": str(sandbox)}}}]},
        {"role": "assistant", "content": "All read."}]
    got = []
    agent, _ = make_agent(llm, tmp_path, backend="llamacpp", system_prompt_path=str(brain),
                          allowed_roots=[str(sandbox.parent)], on_token=lambda k, t: got.append((k, t)))
    res = agent.run("inventory please")
    assert res.text == "All read." and [a["status"] for a in res.actions] == ["ok"]
    second = [b for p, b in llm.requests if p == "/v1/chat/completions"][1]
    tool = [m for m in second["messages"] if m["role"] == "tool"][0]
    assert tool["tool_call_id"] == "c1"


def test_streaming_reports_verify_round(llm, tmp_path):
    llm.script = [final("```dax\nX = 1\n```"), final("### DAX\n```dax\nX = 1\n```")]
    got = []
    agent, _ = make_agent(llm, tmp_path, on_token=lambda k, t: got.append((k, t)))
    res = agent.run("Measure X")
    assert res.issues == []
    assert any(k == "status" and "[verify]" in t for k, t in got)


def test_broken_token_callback_does_not_break_run(llm, tmp_path):
    llm.script = [final("### DAX\n```dax\nX = 1\n```")]

    def boom(kind, text):
        raise RuntimeError("ui died")
    agent, _ = make_agent(llm, tmp_path, on_token=boom)
    assert agent.run("Measure X").text.startswith("### DAX")


# ------------------------------------------------------------------ run_dax_query
class FakeRunner:
    def __init__(self, result=None, error=None):
        self.queries, self.result, self.error = [], result, error

    def __call__(self, query, max_rows):
        self.queries.append((query, max_rows))
        if self.error:
            raise RuntimeError(self.error)
        return self.result


def test_dax_tool_only_offered_with_runner(llm, tmp_path):
    llm.script = [final("ok"), final("ok")]
    make_agent(llm, tmp_path)[0].run("measure?")
    make_agent(llm, tmp_path, dax_runner=FakeRunner({"results": []}))[0].run("measure?")
    bodies = [b for p, b in llm.requests if p == "/api/chat"]
    names = [[t["function"]["name"] for t in b["tools"]] for b in bodies]
    assert "run_dax_query" not in names[0] and "run_dax_query" in names[1]
    tool_line = [ln for ln in bodies[1]["messages"][0]["content"].splitlines() if ln.startswith("You have tools:")][0]
    assert tool_line.endswith("design_dashboard, make_theme, lint_design, svg_measure, run_dax_query.")
    assert "run_dax_query" not in bodies[0]["messages"][0]["content"]


def test_dax_tool_runs_and_formats(llm, tmp_path):
    runner = FakeRunner({"results": [{"columns": ["Date[Year]", "[v]"], "rows": [[2025, "10"], [2026, None]],
                                      "total": 2}]})
    q = "DEFINE MEASURE _Measures[T] = 1\nEVALUATE SUMMARIZECOLUMNS('Date'[Year], \"v\", [T])"
    llm.script = [call("run_dax_query", query=q), final("Tested.")]
    agent, events = make_agent(llm, tmp_path, dax_runner=runner)
    res = agent.run("test my measure")
    assert runner.queries == [(q, 50)]
    tool = [m for p, b in llm.requests if p == "/api/chat" for m in b["messages"] if m["role"] == "tool"][0]
    assert "2025 | 10" in tool["content"] and "(blank)" in tool["content"]
    dax_ev = [e for e in events if e["event"] == "pbi_dax"][0]
    assert dax_ev["rows"] == [2] and "10" not in json.dumps(dax_ev)      # shape only, no values
    assert res.actions[0]["status"] == "ok"


def test_dax_tool_blank_warning_and_errors(llm, tmp_path):
    agent, _ = make_agent(llm, tmp_path, dax_runner=FakeRunner(
        {"results": [{"columns": ["y", "v"], "rows": [[2025, None], [2026, None]]}]}))
    assert "every value is blank" in agent.tool_run_dax_query("EVALUATE ROW(\"v\", BLANK())")
    agent, _ = make_agent(llm, tmp_path, dax_runner=FakeRunner(error="Column 'Foo' cannot be found"))
    assert agent.tool_run_dax_query("EVALUATE {1}").startswith("ERROR: the query failed: Column 'Foo'")


def test_dax_query_violations():
    assert jp.dax_query_violations("EVALUATE {1}") == []
    assert jp.dax_query_violations("// test\nDEFINE MEASURE T[x] = 1 EVALUATE {1}") == []
    assert jp.dax_query_violations("/* c */ evaluate ROW(\"a\", 1)") == []
    assert jp.dax_query_violations("SELECT * FROM $SYSTEM.TMSCHEMA_MEASURES")
    assert jp.dax_query_violations("<Batch><Delete/></Batch>")
    assert jp.dax_query_violations("EVALUATE {1} <Alter>")
    assert jp.dax_query_violations("   ")


def test_dax_tool_refuses_non_queries(llm, tmp_path):
    runner = FakeRunner({"results": []})
    llm.script = [call("run_dax_query", query="<Batch><Refresh/></Batch>"), final("ok")]
    res = make_agent(llm, tmp_path, dax_runner=runner)[0].run("refresh")
    assert runner.queries == [] and res.actions[0]["status"] == "error"


# ------------------------------------------------------------------ citations
CTX = [{"id": "S1", "source": "learn.microsoft.com/dax/divide", "text": "DIVIDE handles divide by zero.",
        "date": "2026-09-01"},
       {"id": "S2", "source": "project:FX.SemanticModel/definition/tables/_Measures.tmdl", "text": "measure X"}]


def test_check_citations():
    assert jp.check_citations("Use DIVIDE [S1] and [S2].", CTX) == []
    issues = jp.check_citations("Use DIVIDE [S1] and [S7].", CTX)
    assert len(issues) == 1 and "[S7]" in issues[0]
    assert jp.check_citations("[S9]", None) == []


def test_fabricated_citation_triggers_fix_round(llm, tmp_path):
    llm.script = [final("DIVIDE is safe [S4]."), final("DIVIDE is safe [S1].")]
    agent, events = make_agent(llm, tmp_path)
    res = agent.run("is DIVIDE safe?", context=CTX)
    assert res.issues == [] and res.text.endswith("[S1].")
    assert any(e["event"] == "pbi_verify_fail" for e in events)
    sysmsg = [b for p, b in llm.requests if p == "/api/chat"][0]["messages"][0]["content"]
    assert "(updated 2026-09-01)" in sysmsg and "project:" in sysmsg


# ------------------------------------------------------------------ modular brain
def test_split_brain_on_real_modelfile():
    pre, sections = jp.split_brain(real_brain())
    assert pre.startswith("You are POWERBI")
    assert [n for n, _ in sections] == list(range(1, 13))


def test_select_brain_dax_question_drops_pbir_section():
    brain = real_brain()
    prompt, included = jp.select_brain(brain, "Write a DAX measure for YTD notional")
    assert 5 in included and 8 not in included and {1, 2, 3, 11} <= set(included)
    assert len(prompt) < len(brain) * 0.6
    assert "were omitted" in prompt


def test_select_brain_page_build_keeps_pbir_and_discovery():
    _, included = jp.select_brain(real_brain(), "Add a new report page with a slicer visual")
    assert {4, 8, 9, 12} <= set(included)


def test_select_brain_design_request_includes_design_system():
    prompt, included = jp.select_brain(real_brain(), "Make my dashboard look modern and polished")
    assert 12 in included and "DASHBOARD DESIGN SYSTEM" in prompt
    _, included = jp.select_brain(real_brain(), "Write a DAX measure for YTD notional")
    assert 12 not in included


def test_select_brain_falls_back_to_full():
    brain = real_brain()
    assert jp.select_brain(brain, "hello")[0] == brain
    assert jp.select_brain("no sections here", "dax")[0] == "no sections here"
    assert {8, 9} <= set(jp.select_brain(brain, "what is this?", has_images=True)[1])


def test_modular_mode_sends_smaller_system_prompt(llm, tmp_path):
    llm.system = real_brain()
    llm.script = [final("### DAX\n```dax\nX = 1\n```"), final("### DAX\n```dax\nX = 1\n```")]
    make_agent(llm, tmp_path)[0].run("DAX measure for YoY")
    agent, events = make_agent(llm, tmp_path, brain_mode="modular")
    agent.run("DAX measure for YoY")
    full, mod = [b["messages"][0]["content"] for p, b in llm.requests if p == "/api/chat"]
    assert len(mod) < len(full) and "8.7 PowerShell writer" in full and "8.7 PowerShell writer" not in mod
    assert any(e["event"] == "pbi_brain" for e in events)


# ------------------------------------------------------------------ schema hook
def test_schema_validator_hook_in_verifier_and_writes(llm, tmp_path, sandbox):
    seen = []

    def validator(doc, where):
        seen.append(where)
        return [f"{where}: schema violation at visual: bad"] if doc.get("visual", {}).get("visualType") == "nope" else []
    vis = {"$schema": SCHEMA_V, "name": HEX_VIS, "position": {"x": 0, "y": 0, "width": 10, "height": 10},
           "visual": {"visualType": "nope"}}
    text = "### FILE: v.json\n```json\n" + json.dumps(vis) + "\n```"
    assert any("schema violation" in i for i in jp.verify_artifacts(text, schema_check=validator))

    agent, _ = make_agent(llm, tmp_path, allowed_roots=[str(sandbox.parent)], schema_validator=validator,
                          approve=lambda a: True)
    page = "1c0a7e3b9d2f4a6c8e10"
    try:
        agent.tool_write_report_files(str(sandbox / "FX.Report"), [
            {"path": f"pages/{page}/visuals/{HEX_VIS}/visual.json", "content": json.dumps(vis)}])
        raise AssertionError("expected ToolError")
    except jp.ToolError as e:
        assert "schema violation" in str(e)
    assert not (sandbox / "FX.Report" / "definition" / "pages" / page / "visuals" / HEX_VIS).exists()
