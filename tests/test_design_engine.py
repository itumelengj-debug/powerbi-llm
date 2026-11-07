"""Dashboard Studio engine: colour science, themes, layout, model resolution, compiler, linter, preview, SVG measures."""
import json
import xml.etree.ElementTree as ET
from itertools import combinations

import pytest

from conftest import FIXTURE
from pbi_design import color as C
from pbi_design import layout as L
from pbi_design import lint as LINT
from pbi_design import pbir as P
from pbi_design import render as R
from pbi_design import svg_measures as S
from pbi_design import themes as T
from pbi_design.compiler import compile_dashboard
from pbi_design.model import FieldError, Model, parse_ref, resolve

REPORT = FIXTURE / "FX.Report"
SPEC = {"title": "FX Desk Overview", "subtitle": "Notional and rates, current year",
        "kpis": [{"measure": "[Notional (ZAR)]", "label": "Notional vs target", "trend": "'Date'[Month]",
                  "target": "[USDZAR Rate]"}, {"measure": "[USDZAR Rate]", "label": "Avg USDZAR"}, "[Notional (ZAR)]"],
        "visuals": [{"type": "line", "title": "Notional by month", "category": "'Date'[Month]", "values": "[Notional (ZAR)]"},
                    {"type": "bar", "title": "Top clients", "category": "'Client'[Client Name]", "values": "[Notional (ZAR)]"},
                    {"type": "table", "title": "Client detail",
                     "columns": ["'Client'[Client Name]", "'Client'[Segment]", "[Notional (ZAR)]"]}],
        "slicers": ["'Date'[Year]", {"field": "'Client'[Segment]", "mode": "Basic"}]}


@pytest.fixture(scope="module")
def model():
    return Model.for_report(REPORT)


# ------------------------------------------------------------------ colour
def test_contrast_and_ciede2000_reference_values():
    assert round(C.contrast("#000000", "#FFFFFF"), 2) == 21.0
    assert round(C.contrast("#777777", "#FFFFFF"), 2) == 4.48
    # Sharma, Wu & Dalal (2005) test pairs 1 and 7
    assert round(C.delta_e2000((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485)), 4) == 2.0425
    assert round(C.delta_e2000((50.0, 0.0, 0.0), (50.0, -1.0, 2.0)), 4) == 2.3669


def test_cvd_simulation_and_contrast_repair():
    assert C.simulate("#FFFFFF", "deutan") == "#FFFFFF" and C.simulate("#000000", "protan") == "#000000"
    assert C.delta_e(C.simulate("#2563EB", "deutan"), C.simulate("#7C3AED", "deutan")) < 2   # classic blue/violet trap
    fixed = C.ensure_contrast("#94A3B8", "#FFFFFF", 3.0)
    assert C.contrast(fixed, "#FFFFFF") >= 3.0 and fixed != "#94A3B8"
    assert C.contrast(C.ensure_contrast("#1D4ED8", "#0B1220", 3.0), "#0B1220") >= 3.0
    with pytest.raises(ValueError):
        C.norm_hex("#12345")


@pytest.mark.parametrize("brand,bg", [("#0F766E", "#FFFFFF"), ("#E4002B", "#FFFFFF"), ("#FFD100", "#FFFFFF"),
                                      ("#0F766E", "#111A2C"), ("#000000", "#111A2C")])
def test_brand_palettes_are_accessible_and_separable(brand, bg):
    pal = C.brand_palette(brand, bg)
    assert len(pal) == 8 and len(set(pal)) == 8
    assert all(C.contrast(c, bg) >= 3.0 for c in pal)
    assert C.min_pairwise(pal[:6])[0] >= 12


# ------------------------------------------------------------------ themes
@pytest.mark.parametrize("preset", list(T.PRESETS))
def test_presets_pass_accessibility(preset):
    t = T.resolve(preset)
    rep = T.theme_report(t)
    assert rep["ok"], rep["warnings"]
    theme = T.build_theme(t)
    assert theme["name"] and len(theme["dataColors"]) == 8
    assert {"callout", "title", "header", "label"} <= set(theme["textClasses"])
    vs = theme["visualStyles"]
    assert vs["*"]["*"]["border"][0]["radius"] == t.radius and vs["page"]["*"]["background"][0]["color"]["solid"]["color"] == t.page
    assert vs["card"]["*"]["categoryLabels"][0]["show"] is False
    assert "expr" not in json.dumps(theme)            # themes take plain values (rulebook 8.2.10)


def test_brand_and_dark_themes():
    t = T.resolve(brand="#C8102E")
    assert t.data[0] == "#C8102E" and T.theme_report(t)["ok"]
    d = T.resolve(brand="#0F766E", mode="dark")
    assert d.mode == "dark" and C.luminance(d.surface) < 0.05 and T.theme_report(d)["ok"]
    with pytest.raises(ValueError):
        T.resolve("neon")
    with pytest.raises(ValueError):
        T.resolve("boardroom", mode="dark")


# ------------------------------------------------------------------ layout
def test_unit_grid_split():
    assert L.split(1232, [2, 1]) == [816, 400]          # two thirds + gutter, one third
    assert L.split(1232, [1, 1, 1]) == [400, 400, 400]
    assert L.split(1232, [1, 1, 1, 1]) == [296, 296, 296, 296]
    assert L.split(1040, [2, 1]) == [688, 336]
    assert sum(L.split(448, [1, 1, 1])) == 448 - 32


def test_executive_reproduces_rulebook_coordinates():
    pl = L.layout_page("executive", 4, ["column"], 0)
    assert [(b.x, b.y, b.w, b.h) for b in pl.kpis] == [(24, 112, 296, 120), (336, 112, 296, 120),
                                                      (648, 112, 296, 120), (960, 112, 296, 120)]
    assert (pl.main[0].x, pl.main[0].y) == (24, 248) and pl.main[0].bottom == 696


def test_every_layout_is_on_grid_inside_and_non_overlapping():
    kinds_all = ["line", "bar", "donut", "table", "column", "scatter", "matrix", "area"]
    for arch in L.ARCHETYPES:
        for nk in range(0, 7):
            for nm in range(1, 9):
                for ns in range(0, 7 if arch == "analytical" else 5):
                    pl = L.layout_page(arch, nk, kinds_all[:nm], ns)
                    boxes = [pl.title] + pl.slicers + pl.kpis + pl.main
                    for b in boxes:
                        assert all(v % 8 == 0 for v in (b.x, b.y, b.w, b.h)), (arch, nk, nm, ns, b)
                        assert b.x >= 24 and b.y >= 16 and b.right <= 1256 and b.bottom <= 696
                    for a, b in combinations(boxes, 2):
                        assert a.right <= b.x or b.right <= a.x or a.bottom <= b.y or b.bottom <= a.y


def test_shape_aware_assignment():
    pl = L.layout_page("executive", 0, ["line", "bar", "table"], 0)
    line, bar, table = pl.main
    assert table.w == 1232                                # tables get a full-width row
    assert line.area > bar.area                           # the primary keeps the larger slot
    pl = L.layout_page("detail", 0, ["column", "table"], 0, primary=1)
    assert pl.main[1].area > pl.main[0].area and pl.main[0].h >= 160
    assert L.choose_archetype(2, ["line"], 6) == "analytical"
    assert L.choose_archetype(0, ["line", "table"], 1) == "detail"
    assert L.choose_archetype(6, ["line"], 1) == "operational"
    with pytest.raises(ValueError):
        L.layout_page("executive", 0, ["line"], 5)


# ------------------------------------------------------------------ model
def test_model_and_field_resolution(model):
    f = resolve("[Notional (ZAR)]", model)
    assert (f.table, f.kind, f.verified, f.format_string) == ("_Measures", "Measure", True, "#,##0")
    assert resolve("'Date'[Month]", model, "Column").timelike
    assert resolve("Client.Client Name", model, "Column").kind == "Column"
    assert resolve({"column": "'Date'[Year]"}, model).kind == "Column"
    with pytest.raises(FieldError, match=r"did you mean _Measures\[Notional \(ZAR\)\]"):
        resolve("_Measures[Notional ZAR]", model)
    with pytest.raises(FieldError):
        resolve("[Revenue]", model)
    assert parse_ref("measure:'My Table'[It''s]") == ("Measure", "My Table", "It''s")
    assert resolve("Sales[Region]", None, "Column").verified is False


# ------------------------------------------------------------------ compiler
def test_compile_against_project(model):
    c = compile_dashboard(SPEC, model, P.detect_versions(REPORT), P.existing_names(REPORT), T.resolve("boardroom"))
    assert c.errors == [] and c.archetype == "executive" and c.unverified == []
    assert c.lint["score"] >= 95 and c.lint["errors"] == 0
    import jo_powerbi as jp
    for rel, doc in c.files.items():
        assert jp._check_json_doc(doc, rel) == [], rel        # passes the adapter's PBIR rules
        name = rel.split("/")[-2] if rel.endswith("visual.json") else rel.split("/")[1]
        assert doc["name"] == name and len(name) == 20
    docs = c.visual_docs()
    assert len(docs) == 9 and len({d["name"] for d in docs}) == 9
    by_type = {d["visual"]["visualType"]: d for d in docs}
    assert {"textbox", "slicer", "kpi", "card", "lineChart", "clusteredBarChart", "tableEx"} <= set(by_type)
    bar = by_type["clusteredBarChart"]["visual"]
    assert bar["query"]["sortDefinition"]["sort"][0]["direction"] == "Descending"
    kpi = by_type["kpi"]["visual"]["query"]["queryState"]
    assert set(kpi) == {"Indicator", "TrendLine", "Goal"}
    for d in docs:
        vco = d["visual"].get("visualContainerObjects")
        if vco:
            assert "padding" in vco                          # rulebook 8.2.11
        if d["visual"]["visualType"] != "textbox":
            assert vco["general"][0]["properties"]["altText"]
    tabs = [d["position"]["tabOrder"] for d in sorted(docs, key=lambda d: (d["position"]["y"], d["position"]["x"]))]
    assert tabs == sorted(tabs)
    assert c.page_doc()["$schema"].endswith("/page/2.0.0/schema.json")


def test_compile_is_deterministic_and_reports_orphans(model, tmp_path):
    a = compile_dashboard(SPEC, model)
    b = compile_dashboard(SPEC, model)
    assert list(a.files) == list(b.files)
    existing = {a.page_name: {"displayName": SPEC["title"], "visuals": {"0123456789abcdef0123"}}}
    c = compile_dashboard(SPEC, model, existing=existing)
    assert c.page_name == a.page_name and c.orphans == ["0123456789abcdef0123"]


def test_compile_errors_are_specific(model):
    bad = {"title": "", "visuals": [{"type": "radar"}, {"type": "line", "category": "'Date'[Month]"}]}
    errs = compile_dashboard(bad, model).errors
    assert any("title is required" in e for e in errs)
    assert any("'radar' is not supported" in e for e in errs)
    assert any("needs 'values'" in e for e in errs)
    errs = compile_dashboard({"title": "T", "kpis": ["[Revenue]"], "slicers": ["[Notional (ZAR)]"]}, model).errors
    assert any("[Revenue] is not in the model" in e for e in errs)
    assert any("slicers need a column" in e for e in errs)
    errs = compile_dashboard({"title": "T", "kpis": ["[Notional (ZAR)]"] * 7}, model).errors
    assert any("at most 6" in e for e in errs)
    errs = compile_dashboard({"title": "T", "layout": "executive", "kpis": ["[Notional (ZAR)]"],
                              "slicers": ["'Date'[Year]"] * 5}, model).errors
    assert any("analytical" in e for e in errs)


def test_compile_without_model_flags_unverified_names():
    c = compile_dashboard({"title": "T", "kpis": ["Sales[Revenue]"],
                           "visuals": [{"type": "column", "category": "Product[Category]", "values": "Sales[Revenue]"}]})
    assert c.ok and "Sales[Revenue]" not in c.unverified and "[Revenue]" in c.unverified
    assert any("not checked against a model" in w for w in c.warnings)


# ------------------------------------------------------------------ linter
def _vis(name, vtype, x, y, w, h, title="T", alt="A", tab=None):
    vco = {}
    if title:
        vco["title"] = [{"properties": {"show": P.lit(True), "text": P.lit(title)}}]
    if alt:
        vco["general"] = [{"properties": {"altText": P.lit(alt)}}]
    pos = {"x": x, "y": y, "width": w, "height": h}
    if tab is not None:
        pos["tabOrder"] = tab
    return {"name": name, "position": pos, "visual": {"visualType": vtype, "query": {"queryState": {}},
                                                      "visualContainerObjects": vco}}


def test_lint_flags_layout_defects():
    page = {"width": 1280, "height": 720}
    bad = [_vis("a", "lineChart", 24, 100, 600, 300), _vis("b", "clusteredBarChart", 500, 120, 400, 300),
           _vis("c", "pieChart", 1100, 500, 300, 300, title=None, alt=None),
           _vis("d", "card", 24, 430, 203, 101)]
    res = LINT.lint_page(page, bad)
    rules = {i["rule"] for i in res["issues"]}
    assert {"overlap", "off_canvas", "off_grid", "missing_title", "missing_alt", "pie"} <= rules
    assert res["errors"] >= 2 and res["score"] < 70 and res["grade"] in ("D", "F")


def test_lint_near_miss_gutters_and_tab_order():
    page = {"width": 1280, "height": 720}
    docs = [_vis("t", "textbox", 24, 16, 400, 48, title=None, alt=None, tab=1000),
            _vis("a", "lineChart", 24, 112, 600, 300, tab=3000), _vis("b", "clusteredBarChart", 640, 116, 616, 300, tab=2000),
            _vis("c", "tableEx", 24, 440, 600, 256, tab=4000), _vis("d", "tableEx", 664, 440, 592, 256, tab=5000)]
    rules = [i["rule"] for i in LINT.lint_page(page, docs)["issues"]]
    assert "near_miss" in rules and "gutters" in rules and "tab_order" in rules


def test_lint_counts_clutter_once_with_weight():
    docs = [_vis(f"v{i}", "card", 24 + (i % 5) * 248, 112 + (i // 5) * 136, 232, 120) for i in range(10)]
    res = LINT.lint_page({"width": 1280, "height": 720}, docs)
    clutter = [i for i in res["issues"] if i["rule"] == "clutter"]
    assert len(clutter) == 1 and clutter[0]["times"] == 2


def test_lint_report_on_fixture():
    res = LINT.lint_report(REPORT)
    assert len(res["pages"]) == 1 and res["pages"][0]["displayName"] == "Overview"
    assert any(i["rule"] == "missing_alt" for i in res["pages"][0]["issues"])
    with pytest.raises(FileNotFoundError):
        LINT.lint_report(FIXTURE)


def test_lint_theme_checks():
    bad = T.Tokens(name="bad", mode="light", page="#FFFFFF", surface="#FFFFFF", surface_alt="#FFFFFF",
                   border="#EEEEEE", gridline="#EEEEEE", text="#BBBBBB", muted="#CCCCCC", accent="#DDDDDD",
                   data=["#EEEEEE", "#EFEFEF"], good="#00FF00", bad="#FF0000", neutral="#FFFF00")
    rules = {i["rule"] for i in LINT.lint_page({}, [], tokens=bad)["issues"]}
    assert {"theme_contrast", "theme_palette"} <= rules


# ------------------------------------------------------------------ preview
def test_render_preview_is_valid_svg_with_theme_and_titles(model):
    t = T.resolve("midnight")
    c = compile_dashboard(SPEC, model, tokens=t)
    svg = R.render_page_svg(c.page_doc(), c.visual_docs(), t)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") and root.attrib["viewBox"] == "0 0 1280 720"
    assert t.page in svg and t.surface in svg and t.data[0] in svg
    for title in ("FX Desk Overview", "Notional by month", "Top clients", "Client detail", "Avg USDZAR"):
        assert title in svg
    assert svg.count("Notional vs target") == 1                     # KPI label drawn once
    html = R.render_html(svg, "FX", c.lint, t)
    assert html.startswith("<!doctype html>") and "Design score" in html


def test_render_escapes_text():
    page = {"width": 1280, "height": 720}
    doc = _vis("x", "lineChart", 24, 24, 400, 300, title="<b>P&L</b>")
    svg = R.render_page_svg(page, [doc], T.resolve("boardroom"))
    ET.fromstring(svg)
    assert "&lt;b&gt;P&amp;L&lt;/b&gt;" in svg


# ------------------------------------------------------------------ SVG measures
@pytest.mark.parametrize("kind,kw", [
    ("sparkline", dict(measure="_Measures[Notional (ZAR)]", axis="'Date'[Date]")),
    ("progress", dict(value="[Notional (ZAR)]", target="[Target]")),
    ("bullet", dict(value="[Notional (ZAR)]", target="[Target]")),
    ("delta_badge", dict(actual="[Notional (ZAR)]", comparison="[Notional PY]")),
    ("colour_rule", dict(measure="[Variance]")),
])
def test_svg_measures_follow_the_rules(kind, kw):
    expr = S.generate(kind, **kw)
    assert S.check_dax(expr) == []
    if kind != "colour_rule":
        assert expr.count("data:image/svg+xml;utf8,") == 1
        assert "'#" not in expr and "%23" in expr
        assert 'BLANK' in expr or "IF (" in expr
    if kind in ("sparkline", "progress", "bullet"):
        assert '"en-US"' in expr                                    # locale-proof coordinates
    if kind == "delta_badge":
        assert '"%", "%25"' in expr and "UNICHAR ( 9650 )" in expr


def test_svg_measure_tmdl_and_checks():
    script = S.tmdl_script("_Measures", "Notional Spark", S.sparkline("[Notional (ZAR)]", "'Date'[Date]"),
                           description="trend")
    lines = script.splitlines()
    assert lines[0] == "createOrReplace" and lines[2] == "\tref table _Measures"
    assert "\t\t/// trend" in lines and "\t\tmeasure 'Notional Spark' =" in lines
    assert "\t\t\tdataCategory: ImageUrl" in lines and not any(ln.startswith("  ") for ln in lines)
    assert S.check_dax("IF ( (1, 2 )") == ["unbalanced parentheses"]
    assert S.check_dax("\"data:image/svg+xml;utf8,<rect fill='#FFF'/>\"") == ["raw '#' in an SVG data URI - write %23"]
    with pytest.raises(ValueError):
        S.generate("radar", measure="[X]")
    with pytest.raises(ValueError):
        S.sparkline("[X]", "NotAColumn")
