"""Benchmark: checks against known answers, model and agent runs against the mock, compare."""
import json

import bench
import tasks as T
from checks import RunResult, citations_valid, first_measure, jsons
from conftest import call, final

HEX_PAGE, HEX_VIS = "4b1d7e20c9a35f86d2e0", "9e27c4a1f06b3d85e7a2"
SV = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.9.0/schema.json"


def task(tid):
    return next(t for t in T.TASKS if t.id == tid)


def ok(tid, text, **kw):
    return bench.score(task(tid), RunResult(text=text, **kw))["pass"]


def test_task_ids_unique_and_ported_originals_present():
    ids = [t.id for t in T.TASKS]
    assert len(ids) == len(set(ids)) and len(ids) >= 50
    for orig in ["01-dax-yoy", "02-dax-share", "03-m-csv", "04-tmdl-view", "05-pbir-visual", "06-pbir-page",
                 "07-theme", "08-rls", "09-refresh-api", "10-udf", "11-safety", "12-scope"]:
        assert orig in ids
    assert {t.level for t in T.TASKS} == {"core", "hard"}


def test_checks_on_known_answers():
    assert ok("01-dax-yoy", "### DAX\n```dax\nYoY = DIVIDE([N] - CALCULATE([N], SAMEPERIODLASTYEAR('Date'[Date])), "
                            "CALCULATE([N], SAMEPERIODLASTYEAR('Date'[Date])))\n```")
    assert not ok("01-dax-yoy", "### DAX\n```dax\nYoY = [N] / [PY]\n```")
    assert ok("12-scope", "Out of scope - I only do Power BI.")
    assert not ok("scope-sql-in-scope", "Out of scope - I only do Power BI.")
    assert ok("scope-sql-in-scope", "### SQL\n```sql\nCREATE VIEW dbo.vTrades AS SELECT 1 AS x;\n```")
    assert ok("ground-fake-function", "Unverified: ROLLINGMEDIANX is not a DAX function I can confirm.")
    assert not ok("ground-fake-function", "### DAX\n```dax\nM = ROLLINGMEDIANX(T, [x], 3)\n```")
    assert ok("dax-perf-filter", "### DAX\n```dax\nUSD = CALCULATE ( SUM ( Trades[NotionalZAR] ), "
                                 "KEEPFILTERS ( Trades[CurrencyPair] = \"USDZAR\" ) )\n```")
    assert not ok("dax-perf-filter", "### DAX\n```dax\nUSD = CALCULATE(SUMX(FILTER(Trades, 1), 1))\n```")
    assert ok("safety-secret", "### POWERSHELL\n```powershell\n$c = Get-Credential\n```")
    assert not ok("safety-secret", "### POWERSHELL\n```powershell\n$s = 'abc123XYZsecret'\n```")
    tm = "### TMDL VIEW\n```tmdl\ncreateOrReplace\n\tref table _Measures\n" + "".join(
        f"\t\t/// d\n\t\tmeasure M{i} = 1\n\t\t\tdisplayFolder: Stats\n" for i in range(3)) + "```"
    assert ok("tmdl-measure-batch", tm)


def test_visual_check_and_first_measure():
    vis = {"$schema": SV, "name": HEX_VIS, "position": {"x": 0, "y": 0, "width": 1, "height": 1},
           "visual": {"visualType": "clusteredColumnChart", "query": {"queryState": {
               "Category": {"projections": [{"field": {"Column": {}}, "queryRef": "Date.Month", "nativeQueryRef": "Month"}]},
               "Y": {"projections": [{"field": {"Measure": {}}, "queryRef": "_Measures.N", "nativeQueryRef": "N"}]}}}}}
    assert ok("05-pbir-visual", "### FILE: x\n```json\n" + json.dumps(vis) + "\n```")
    vis["visual"]["query"]["queryState"]["Y"]["projections"][0].pop("nativeQueryRef")
    assert not ok("05-pbir-visual", "```json\n" + json.dumps(vis) + "\n```")
    assert first_measure("### DAX\n```dax\n// c\n[Share] := DIVIDE ( 1, 2 )\n```") == ("Share", "DIVIDE ( 1, 2 )")
    assert first_measure("```dax\nEVALUATE {1}\n```") is None
    assert jsons(RunResult(text="@'\n{\"a\": 1}\n'@", written={"x.json": {"b": 2}})) == [{"a": 1}, {"b": 2}]


def test_citations_valid():
    ctx = [{"id": "S1", "source": "a", "text": "t"}]
    assert citations_valid(RunResult(text="yes [S1]", context=ctx))[0]
    assert not citations_valid(RunResult(text="yes [S3]", context=ctx))[0]
    assert not citations_valid(RunResult(text="yes", context=ctx))[0]
    assert citations_valid(RunResult(text="Unverified: x", context=ctx))[0]


def test_dax_exec_scoring():
    t = task("01-dax-yoy")
    text = "### DAX\n```dax\nYoY = DIVIDE ( 1, SAMEPERIODLASTYEAR ( 'Date'[Date] ) )\n```"
    seen = []
    good = lambda q, n: seen.append(q) or {"results": [{"columns": ["y", "v"], "rows": [[2025, "0.1"]]}]}
    assert bench.score(t, RunResult(text=text), dax_runner=good)["pass"]
    assert seen[0].startswith("DEFINE MEASURE _Measures[__bench] = DIVIDE ( 1,") and "SUMMARIZECOLUMNS" in seen[0]
    blank = lambda q, n: {"results": [{"columns": ["y", "v"], "rows": [[2025, None]]}]}
    assert bench.score(t, RunResult(text=text), dax_runner=blank)["reason"] == "dax exec: every value blank"

    def err(q, n):
        raise RuntimeError("Column 'Date' not found")
    assert "not found" in bench.score(t, RunResult(text=text), dax_runner=err)["reason"]


def test_model_mode_run_and_compare(llm, tmp_path, capsys):
    llm.default = final("Out of scope - I only do Power BI.")
    out = tmp_path / "runs"
    assert bench.main(["--host", llm.url, "--only", "scope", "--repeats", "2", "--out", str(out)]) == 0
    run_dir = next(out.iterdir())
    s = json.loads((run_dir / "summary.json").read_text())
    assert s["tasks"]["12-scope"]["rate"] == 1.0 and s["tasks"]["scope-sql-in-scope"]["rate"] == 0.0
    assert s["meta"]["repeats"] == 2 and (run_dir / "12-scope-r2.md").exists() and (run_dir / "summary.md").exists()
    assert s["median_tok_s"] == 100.0

    llm.default = final("### SQL\n```sql\nSELECT 1\n```")
    assert bench.main(["--host", llm.url, "--only", "scope", "--repeats", "1", "--out", str(out),
                       "--brain", "modular"]) == 0
    b = next(p for p in out.iterdir() if "modular" in p.name)
    capsys.readouterr()
    bench.main(["--compare", str(run_dir / "summary.json"), str(b / "summary.json")])
    cmp_out = capsys.readouterr().out
    assert "scope-sql-in-scope: 0% -> 100%" in cmp_out and "12-scope: 100% -> 0%" in cmp_out


def test_agent_mode_writes_land_in_sandbox(llm, tmp_path):
    page = {"$schema": SV.replace("visualContainer/2.9.0", "page/2.0.0"), "name": HEX_PAGE,
            "displayName": "Client Drilldown", "displayOption": "FitToPage", "height": 720, "width": 1280}
    table = {"$schema": SV, "name": HEX_VIS, "position": {"x": 24, "y": 96, "z": 0, "width": 600, "height": 400},
             "visual": {"visualType": "tableEx", "query": {"queryState": {"Values": {"projections": [
                 {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Client"}}, "Property": "Client Name"}},
                  "queryRef": "Client.Client Name", "nativeQueryRef": "Client Name"}]}}}}}

    def write_call(body):
        report = body["messages"][-1]["content"].split("report is at ")[1].split(" (PBIR)")[0]
        return call("write_report_files", report_path=report, files=[
            {"path": f"pages/{HEX_PAGE}/page.json", "content": json.dumps(page)},
            {"path": f"pages/{HEX_PAGE}/visuals/{HEX_VIS}/visual.json", "content": json.dumps(table)}])
    llm.script = [write_call, final("Wrote the page. Reopen the .pbip.")]
    out = tmp_path / "runs"
    assert bench.main(["--host", llm.url, "--mode", "agent", "--only", "06-pbir-page", "--repeats", "1",
                       "--out", str(out), "--no-retrieval"]) == 0
    s = json.loads((next(out.iterdir()) / "summary.json").read_text())
    assert s["tasks"]["06-pbir-page"]["rate"] == 1.0


def test_list(capsys):
    assert bench.main(["--list", "--category", "safety"]) == 0
    assert "4 task(s)" in capsys.readouterr().out


# ------------------------------------------------------------------ design tasks
def test_design_checks_on_known_answers():
    good_plan = "\n".join(["- title x 24, y 16, w 848, h 80", "- slicer x 888, y 16, w 368, h 80",
                           "- card x 24, y 112, w 296, h 120", "- card x 336, y 112, w 296, h 120",
                           "- card x 648, y 112, w 296, h 120", "- card x 960, y 112, w 296, h 120",
                           "- trend x 24, y 248, w 816, h 448", "- breakdown x 856, y 248, w 400, h 448"])
    assert ok("design-exec-layout", good_plan)
    assert not ok("design-exec-layout", good_plan.replace("x 336, y 112", "x 300, y 112"))     # overlap + off grid
    assert not ok("design-exec-layout", "Put the cards at the top and the chart below.")
    theme = {"name": "T", "background": "#FFFFFF", "foreground": "#111827",
             "dataColors": ["#0F766E", "#2563EB", "#C2410C", "#7C3AED", "#BE123C", "#A16207"]}
    assert ok("design-brand-theme", "### THEME\n```json\n" + json.dumps(theme) + "\n```")
    theme["dataColors"][1] = "#93C5FD"                                                         # 1.9:1 on white
    assert not ok("design-brand-theme", "### THEME\n```json\n" + json.dumps(theme) + "\n```")
    spark = ("### DAX\n```dax\nS = \"data:image/svg+xml;utf8,<svg><polyline stroke='%23123456' points='\" & "
             "CONCATENATEX ( T, [x] ) & \"'/></svg>\"\n```\nSet the data category to Image URL.")
    assert ok("design-svg-sparkline", spark)
    assert not ok("design-svg-sparkline", spark.replace("%23", "#"))
    assert ok("design-critique", "Cards A and B overlap and their tops are not aligned to the grid; the pie with 9 "
                                 "slices should be a sorted bar; add titles.")
    assert ok("design-kpi-context", "Show the value, the variance vs target with an arrow, and a sparkline trend.")


def test_design_score_in_summary_and_agent_build(llm, tmp_path):
    spec = {"title": "Desk Overview", "kpis": ["[Notional (ZAR)]", "[USDZAR Rate]"],
            "visuals": [{"type": "line", "title": "Notional by month", "category": "'Date'[Month]", "values": "[Notional (ZAR)]"},
                        {"type": "bar", "title": "Clients by notional", "category": "'Client'[Client Name]",
                         "values": "[Notional (ZAR)]"}],
            "slicers": ["'Date'[Year]"]}

    def design_call(body):
        report = body["messages"][-1]["content"].split(" in ")[1].split(":")[0]
        return call("design_dashboard", spec=spec, report_path=report)
    llm.script = [design_call, final("Built 'Desk Overview'. Design score 100/100.")]
    out = tmp_path / "runs"
    assert bench.main(["--host", llm.url, "--mode", "agent", "--only", "design-build-exec", "--repeats", "1",
                       "--out", str(out), "--no-retrieval"]) == 0
    s = json.loads((next(out.iterdir()) / "summary.json").read_text())
    assert s["tasks"]["design-build-exec"]["rate"] == 1.0 and s["design_score"] >= 90
