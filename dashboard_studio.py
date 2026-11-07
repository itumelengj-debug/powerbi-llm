r"""
dashboard_studio.py - the design engine from the command line.

    python dashboard_studio.py themes                                   # presets + accessibility checks
    python dashboard_studio.py theme --brand "#0F766E" -o brand.json    # theme file to import in Desktop
    python dashboard_studio.py build spec.json --report C:\PBI\FX\FX.Report --theme midnight
    python dashboard_studio.py build spec.json --report C:\PBI\FX\FX.Report --write   # asks before writing
    python dashboard_studio.py lint C:\PBI\FX\FX.Report                 # design score per page (any report)
    python dashboard_studio.py preview C:\PBI\FX\FX.Report -o overview.html
    python dashboard_studio.py gallery -o gallery.html                  # every archetype in every preset
    python dashboard_studio.py svg-measure sparkline --measure "[Notional (ZAR)]" --axis "'Date'[Date]"
    python dashboard_studio.py review screenshot.png                    # the local model critiques a page

build writes, under --out (default studio-out/<page>): the PBIR files, preview.svg / preview.html,
lint.json and write-page.ps1 (backup + Desktop-closed check + pages.json registration).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "agent-jo"))

from pbi_design import lint as LINT  # noqa: E402
from pbi_design import pbir as PBIR  # noqa: E402
from pbi_design import render as R  # noqa: E402
from pbi_design import svg_measures as SVG  # noqa: E402
from pbi_design import themes as T  # noqa: E402
from pbi_design.compiler import compile_dashboard  # noqa: E402
from pbi_design.model import Model  # noqa: E402

REVIEW_PROMPT = (
    "Critique this Power BI report page as a senior report designer. Score each area 1-5 with one line of "
    "evidence from the image: visual hierarchy and layout, alignment and spacing, chart choice, colour and "
    "contrast, typography and titles, clutter, accessibility. Then list the five highest-impact fixes as "
    "concrete changes (exact positions, formatting settings, chart swaps or a design_dashboard spec), and say "
    "what cannot be judged from a screenshot.")

SAMPLE_SPECS = {
    "executive": {"title": "Sales Overview", "subtitle": "Current year, all regions",
                  "kpis": ["Sales[Revenue]", "Sales[Margin %]", "Sales[Orders]", "Sales[Customers]"],
                  "visuals": [{"type": "line", "title": "Revenue by month", "category": "'Date'[Month]", "values": "Sales[Revenue]"},
                              {"type": "bar", "title": "Top products", "category": "Product[Product]", "values": "Sales[Revenue]"},
                              {"type": "donut", "title": "Revenue by channel", "category": "Channel[Channel]", "values": "Sales[Revenue]"}],
                  "slicers": ["'Date'[Year]", "Region[Region]"]},
    "operational": {"title": "Operations Monitor", "layout": "operational",
                    "kpis": ["Ops[Open Tickets]", "Ops[SLA %]", "Ops[Backlog]", "Ops[Escalations]"],
                    "visuals": [{"type": "column", "title": "Tickets by day", "category": "'Date'[Date]", "values": "Ops[Tickets]"},
                                {"type": "bar", "title": "Backlog by team", "category": "Team[Team]", "values": "Ops[Backlog]"},
                                {"type": "line", "title": "SLA % trend", "category": "'Date'[Date]", "values": "Ops[SLA %]"},
                                {"type": "table", "title": "Oldest open tickets", "columns": ["Ticket[Ticket]", "Team[Team]", "Ops[Avg Age]"]}],
                    "slicers": ["Team[Team]", "'Date'[Month]"]},
    "analytical": {"title": "Margin Explorer", "layout": "analytical",
                   "kpis": ["Sales[Revenue]", "Sales[Margin %]", "Sales[Discount %]"],
                   "visuals": [{"type": "scatter", "title": "Margin vs discount by product", "details": "Product[Product]",
                                "x": "Sales[Discount %]", "y": "Sales[Margin %]"},
                               {"type": "waterfall", "title": "Margin bridge", "category": "Driver[Driver]", "values": "Sales[Margin]"},
                               {"type": "matrix", "title": "Margin by category and region", "rows": "Product[Category]",
                                "columns": "Region[Region]", "values": "Sales[Margin %]"}],
                   "slicers": ["'Date'[Year]", "Region[Region]", "Product[Category]", "Channel[Channel]", "Customer[Segment]"]},
    "detail": {"title": "Customer Detail", "layout": "detail",
               "kpis": ["Sales[Revenue]", "Sales[Orders]", "Sales[Avg Order]"],
               "visuals": [{"type": "table", "title": "Customers", "columns": ["Customer[Customer]", "Customer[Segment]", "Sales[Revenue]", "Sales[Orders]"]},
                           {"type": "column", "title": "Revenue by month", "category": "'Date'[Month]", "values": "Sales[Revenue]"}],
               "slicers": ["Customer[Segment]", "'Date'[Year]"]},
}


def _tokens(a) -> T.Tokens:
    return T.resolve(getattr(a, "theme", "") or "boardroom", brand=getattr(a, "brand", "") or "",
                     mode=getattr(a, "mode", "") or "")


def _print(text: str = ""):
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def cmd_themes(a) -> int:
    for key in T.PRESETS:
        t = T.resolve(key)
        rep = T.theme_report(t)
        _print(f"{key:<10} {t.mode:<5} {'pass' if rep['ok'] else 'CHECK'}  {' '.join(t.data[:6])}")
        for w in rep["warnings"]:
            _print(f"           ! {w}")
    return 0


def cmd_theme(a) -> int:
    t = T.resolve(a.preset or ("" if a.brand else "boardroom"), brand=a.brand or "", mode=a.mode or "",
                  font=a.font or "", name=a.name or "")
    rep = T.theme_report(t)
    out = Path(a.out or f"{t.name.lower().replace(' ', '-')}.json")
    out.write_text(json.dumps(T.build_theme(t), indent=2) + "\n", encoding="utf-8")
    _print(f"Theme '{t.name}' ({t.mode}) -> {out}")
    for c in rep["checks"]:
        _print(f"  {'ok ' if c['ok'] else 'LOW'} {c['check']}: {c['ratio']}:1 (needs {c['needs']}:1)")
    _print("  palette: " + " ".join(t.data))
    for w in rep["warnings"] + rep["notes"]:
        _print(f"  - {w}")
    _print("Import: Power BI Desktop > View > Themes > Browse for themes")
    return 0 if rep["ok"] else 1


def _load_model(a, report: Path = None):
    if getattr(a, "model", None):
        p = Path(a.model)
        sm = p if p.name.endswith(".SemanticModel") else next(iter(sorted(p.glob("*.SemanticModel"))), None)
        return Model.from_semantic_model(sm) if sm else None
    return Model.for_report(report) if report else None


def cmd_build(a) -> int:
    spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    tokens = _tokens(a)
    report = Path(a.report).resolve() if a.report else None
    model = _load_model(a, report)
    versions = PBIR.detect_versions(report) if report else None
    existing = PBIR.existing_names(report) if report else None
    c = compile_dashboard(spec, model, versions, existing, tokens)
    if c.errors:
        _print("The spec needs fixing:")
        for e in c.errors:
            _print(f"  - {e}")
        return 2
    out = Path(a.out or Path("studio-out") / c.page_name)
    for rel, doc in c.files.items():
        f = out / "definition" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    svg = R.render_page_svg(c.page_doc(), c.visual_docs(), tokens)
    (out / "preview.svg").write_text(svg, encoding="utf-8")
    (out / "preview.html").write_text(R.render_html(svg, c.display_name, c.lint, tokens), encoding="utf-8")
    (out / "lint.json").write_text(json.dumps(c.lint, indent=2), encoding="utf-8")
    (out / "theme.json").write_text(json.dumps(T.build_theme(tokens), indent=2) + "\n", encoding="utf-8")
    from jo_powerbi import _writer_script
    (out / "write-page.ps1").write_text(_writer_script(c, str(report) if report else ""), encoding="utf-8-sig")
    _print(f"Page '{c.display_name}' ({c.archetype}, {len(c.placements)} visuals) -> {out}")
    for p in c.placements:
        _print(f"  {p['type']:<10} {p['title'][:40]:<40} x {p['x']:>4} y {p['y']:>3} {p['width']}x{p['height']}")
    _print(LINT.summary(c.lint))
    for w in c.warnings:
        _print(f"  note: {w}")
    if c.orphans:
        _print("  earlier build left these visuals (delete their folders): " + ", ".join(c.orphans))
    _print(f"Preview: {out / 'preview.html'}   Theme: {out / 'theme.json'}")
    if a.write:
        if not report:
            _print("--write needs --report")
            return 2
        from jo_powerbi import PowerBIAgent, PowerBIConfig, ToolError
        from powerbi_agent import ask_approval
        agent = PowerBIAgent(PowerBIConfig(allowed_roots=[str(report.parent)]), approve=ask_approval)
        files = [{"path": rel, "content": json.dumps(doc, ensure_ascii=False)} for rel, doc in c.files.items()]
        try:
            _print(agent.tool_write_report_files(str(report), files))
        except ToolError as e:
            _print(f"ERROR: {e}")
            return 1
    return 0


def cmd_lint(a) -> int:
    tokens = _tokens(a) if (a.theme or a.brand) else None
    res = LINT.lint_report(a.report, tokens, only_page=a.page)
    if a.json:
        _print(json.dumps(res, indent=2))
        return 0 if all(p["errors"] == 0 for p in res["pages"]) else 1
    _print(f"Report design score {res['score']}/100 (grade {res['grade']})")
    for p in res["pages"]:
        _print(f"\n{p['displayName']}: " + LINT.summary(p, a.limit))
    for c in res["cross_page"]:
        _print(f"[{c['severity']}] {c['message']}")
    return 0 if all(p["errors"] == 0 for p in res["pages"]) else 1


def cmd_preview(a) -> int:
    tokens = _tokens(a)
    pages = Path(a.report) / "definition" / "pages"
    dirs = [d for d in sorted(pages.iterdir()) if (d / "page.json").exists()]
    page_dir = next((d for d in dirs if a.page in (None, d.name)
                     or json.loads((d / "page.json").read_text(encoding="utf-8-sig")).get("displayName") == a.page), None)
    if page_dir is None:
        _print(f"no page '{a.page}'")
        return 2
    page, visuals = LINT.load_page(page_dir)
    svg = R.render_page_svg(page, visuals, tokens)
    out = Path(a.out or f"{page_dir.name}.html")
    if out.suffix == ".svg":
        out.write_text(svg, encoding="utf-8")
    else:
        out.write_text(R.render_html(svg, page.get("displayName", page_dir.name), LINT.lint_page(page, visuals, tokens), tokens),
                       encoding="utf-8")
    _print(f"Preview of '{page.get('displayName', page_dir.name)}' -> {out}")
    return 0


def gallery_html(presets=None) -> str:
    presets = presets or list(T.PRESETS)
    cards = []
    for arch, spec in SAMPLE_SPECS.items():
        for key in presets:
            t = T.resolve(key)
            c = compile_dashboard(spec, None, tokens=t)
            svg = R.render_page_svg(c.page_doc(), c.visual_docs(), t, note="")
            cards.append(f"<figure><figcaption>{arch} &middot; {t.name} &middot; score {c.lint['score']}</figcaption>{svg}</figure>")
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, "
            "initial-scale=1'><title>Dashboard Studio gallery</title><style>body{margin:0;padding:24px;background:#f5f6f8;"
            f"font-family:{R.FONT};color:#0f172a}}h1{{font-size:22px}}main{{display:grid;grid-template-columns:repeat(auto-fill,"
            "minmax(560px,1fr));gap:24px}}figure{margin:0}figcaption{font-size:13px;margin:0 0 8px;color:#475569}"
            "svg{width:100%;height:auto;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.12)}</style></head><body>"
            "<h1>Dashboard Studio gallery</h1><p>Every layout archetype in every theme preset (sample shapes, not data).</p>"
            "<main>" + "".join(cards) + "</main></body></html>")


def cmd_gallery(a) -> int:
    out = Path(a.out or "gallery.html")
    out.write_text(gallery_html(a.preset or None), encoding="utf-8")
    _print(f"Gallery -> {out}")
    return 0


def cmd_svg_measure(a) -> int:
    kw = {"sparkline": dict(measure=a.measure, axis=a.axis or ""),
          "progress": dict(value=a.measure, target=a.target or ""),
          "bullet": dict(value=a.measure, target=a.target or ""),
          "delta_badge": dict(actual=a.measure, comparison=a.comparison or ""),
          "colour_rule": dict(measure=a.measure)}[a.kind]
    missing = [k for k, v in kw.items() if not v]
    if missing:
        _print(f"{a.kind} needs --{missing[0].replace('value', 'measure').replace('actual', 'measure')}")
        return 2
    expr = SVG.generate(a.kind, **kw)
    name = a.name or f"{a.measure.strip('[]').split('[')[-1].rstrip(']')} {a.kind.replace('_', ' ').title()}"
    _print(SVG.tmdl_script(a.table, name, expr, image=a.kind != "colour_rule"))
    return 0


def cmd_review(a) -> int:
    from jo_powerbi import PowerBIAgent, PowerBIConfig
    from powerbi_agent import OLLAMA_HOST
    agent = PowerBIAgent(PowerBIConfig(model=a.model_name, ollama_url=a.host or OLLAMA_HOST, brain_mode="modular"))
    res = agent.run(REVIEW_PROMPT, images=[a.image], think=True)
    _print(res.text.strip())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Dashboard Studio: design engine for Power BI report pages")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def look(p):
        p.add_argument("--theme", default="", help="preset: " + ", ".join(T.PRESETS))
        p.add_argument("--brand", default="", help="derive the look from a brand colour (#RRGGBB)")
        p.add_argument("--mode", default="", help="light or dark (with --brand)")

    sub.add_parser("themes", help="list presets with accessibility checks")
    p = sub.add_parser("theme", help="write a theme JSON file")
    p.add_argument("--preset", default="")
    p.add_argument("--brand", default="")
    p.add_argument("--mode", default="")
    p.add_argument("--font", default="")
    p.add_argument("--name", default="")
    p.add_argument("-o", "--out", default="")
    p = sub.add_parser("build", help="compile a spec into PBIR files + preview + writer script")
    p.add_argument("spec")
    p.add_argument("--report", default="", help="<Name>.Report folder: checks names and reuses $schema versions")
    p.add_argument("--model", default="", help="a .SemanticModel folder (or a project folder) to check names against")
    p.add_argument("--out", default="")
    p.add_argument("--write", action="store_true", help="write into --report after confirmation (backup first)")
    look(p)
    p = sub.add_parser("lint", help="design score for a PBIR report")
    p.add_argument("report")
    p.add_argument("--page", default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=8)
    look(p)
    p = sub.add_parser("preview", help="render a report page to HTML or SVG")
    p.add_argument("report")
    p.add_argument("--page", default=None)
    p.add_argument("-o", "--out", default="")
    look(p)
    p = sub.add_parser("gallery", help="every archetype in every preset, as one HTML page")
    p.add_argument("-o", "--out", default="")
    p.add_argument("--preset", action="append")
    p = sub.add_parser("svg-measure", help="print a TMDL script for an SVG measure")
    p.add_argument("kind", choices=SVG.KINDS)
    p.add_argument("--measure", required=True)
    p.add_argument("--axis", default="")
    p.add_argument("--target", default="")
    p.add_argument("--comparison", default="")
    p.add_argument("--table", default="_Measures")
    p.add_argument("--name", default="")
    p = sub.add_parser("review", help="ask the local model to critique a screenshot of a page")
    p.add_argument("image")
    p.add_argument("--model-name", default="powerbi")
    p.add_argument("--host", default="")
    a = ap.parse_args(argv)
    return {"themes": cmd_themes, "theme": cmd_theme, "build": cmd_build, "lint": cmd_lint, "preview": cmd_preview,
            "gallery": cmd_gallery, "svg-measure": cmd_svg_measure, "review": cmd_review}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
