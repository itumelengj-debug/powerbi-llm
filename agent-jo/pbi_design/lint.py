"""
Design linter: score a PBIR page (or a whole report) 0-100 on the measurable parts of good dashboard
design. It reads plain PBIR, so it grades any report - built by this model, another model or a person.

Errors   overlapping visuals, visuals off the canvas, far too many visuals
Layout   off-grid positions, edges that almost (but not quite) align, inconsistent gutters, cramped gaps,
         uneven KPI tiles, margins
Content  clutter (more than 8 data visuals), pie/donut/gauge overuse, very wide tables, visuals too small
         to read, KPIs below the charts, no page title
Access   missing titles, missing alt text, tab order that does not follow reading order, tiny text
Style    hard-coded colours instead of theme colours; theme contrast and palette (when tokens are given)
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Optional

from . import pbir as P
from .layout import SHAPE

RULES = {   # rule: (severity, points per occurrence, cap)
    "overlap": ("error", 12, 36), "off_canvas": ("error", 12, 36), "too_many": ("error", 15, 15),
    "off_grid": ("warn", 2, 12), "margin": ("warn", 3, 9), "near_miss": ("warn", 3, 15),
    "gutters": ("warn", 4, 12), "tight_gap": ("warn", 4, 12), "kpi_sizes": ("warn", 4, 8),
    "clutter": ("warn", 5, 20), "missing_title": ("warn", 3, 15), "missing_alt": ("warn", 2, 12),
    "tab_order": ("warn", 4, 4), "pie_overuse": ("warn", 4, 4), "pie": ("info", 1, 3),
    "wide_table": ("warn", 3, 6), "small": ("warn", 3, 12), "cramped": ("warn", 4, 4),
    "sparse": ("info", 2, 2), "hardcoded_colours": ("warn", 3, 3), "tiny_text": ("warn", 2, 6),
    "hierarchy": ("info", 2, 2), "no_page_title": ("info", 2, 2), "theme_contrast": ("warn", 5, 15),
    "theme_palette": ("warn", 3, 9),
}
_KIND = {vtype: kind for kind, (vtype, _) in P.VISUALS.items()}
_CARDS = {"card", "multiRowCard", "kpi", "cardVisual"}
_HEX = re.compile(r"'#[0-9A-Fa-f]{6}'")


@dataclass
class Item:
    name: str
    vtype: str
    x: float
    y: float
    w: float
    h: float
    tab: Optional[float]
    title: Optional[str]
    alt: Optional[str]
    doc: dict

    @property
    def right(self):
        return self.x + self.w

    @property
    def bottom(self):
        return self.y + self.h

    @property
    def decorative(self):
        return self.vtype in ("shape", "image", "basicShape")

    @property
    def data(self):
        return self.vtype in P.DATA_VISUAL_TYPES or (self.vtype not in P.DECORATIVE and self.vtype != "slicer"
                                                     and bool((self.doc.get("visual") or {}).get("query")))

    @property
    def slicer(self):
        return "slicer" in self.vtype.lower()


def _literal(objs: dict, obj: str, prop: str):
    try:
        v = objs[obj][0]["properties"][prop]["expr"]["Literal"]["Value"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(v, str) and v.startswith("'") and v.endswith("'"):
        return v[1:-1].replace("''", "'")
    return v


def items_from(visual_docs) -> list:
    out = []
    for d in visual_docs:
        if not isinstance(d, dict) or "visualGroup" in d:
            continue
        pos = d.get("position") or {}
        vis = d.get("visual") or {}
        vco = vis.get("visualContainerObjects") or {}
        show = _literal(vco, "title", "show")
        title = _literal(vco, "title", "text") if show not in ("false", False) else None
        out.append(Item(str(d.get("name", "?")), str(vis.get("visualType") or "?"),
                        float(pos.get("x", 0)), float(pos.get("y", 0)), float(pos.get("width", 0)),
                        float(pos.get("height", 0)), pos.get("tabOrder"), title, _literal(vco, "general", "altText"), d))
    return out


def _overlap_1d(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def lint_page(page: Optional[dict], visual_docs, tokens=None) -> dict:
    page = page or {}
    W, H = float(page.get("width") or 1280), float(page.get("height") or 720)
    items = items_from(visual_docs)
    content = [i for i in items if not i.decorative]
    data = [i for i in content if i.data]
    found = []

    def add(rule, message, names=(), times: int = 1):
        sev, _, _ = RULES[rule]
        found.append({"rule": rule, "severity": sev, "message": message, "visuals": list(names), "times": times})

    # ---- canvas and grid
    for i in content:
        if i.x < 0 or i.y < 0 or i.right > W or i.bottom > H:
            add("off_canvas", f"{i.vtype} '{i.title or i.name}' extends past the {int(W)}x{int(H)} canvas", [i.name])
        elif i.x < 16 or i.y < 8 or i.right > W - 16 or i.bottom > H - 8:
            add("margin", f"{i.vtype} '{i.title or i.name}' sits closer than 16 px to the canvas edge", [i.name])
        if any(v % 8 for v in (i.x, i.y, i.w, i.h)):
            add("off_grid", f"{i.vtype} '{i.title or i.name}' is off the 8 px grid "
                            f"(x {i.x:g}, y {i.y:g}, w {i.w:g}, h {i.h:g})", [i.name])
    for a, b in combinations(content, 2):
        ov = _overlap_1d(a.x, a.right, b.x, b.right) * _overlap_1d(a.y, a.bottom, b.y, b.bottom)
        if ov > 64:
            add("overlap", f"'{a.title or a.name}' and '{b.title or b.name}' overlap", [a.name, b.name])

    # ---- alignment and gutters
    hgaps, vgaps = [], []
    for a, b in combinations(content, 2):
        vshare = _overlap_1d(a.y, a.bottom, b.y, b.bottom) / max(1.0, min(a.h, b.h))
        hshare = _overlap_1d(a.x, a.right, b.x, b.right) / max(1.0, min(a.w, b.w))
        if vshare > 0.5:
            for ea, eb, what in ((a.y, b.y, "tops"), (a.bottom, b.bottom, "bottoms")):
                if 0 < abs(ea - eb) <= 12:
                    add("near_miss", f"{what} of '{a.title or a.name}' and '{b.title or b.name}' are "
                                     f"{abs(ea - eb):g} px apart - align them", [a.name, b.name])
        if hshare > 0.5:
            for ea, eb, what in ((a.x, b.x, "left edges"), (a.right, b.right, "right edges")):
                if 0 < abs(ea - eb) <= 12:
                    add("near_miss", f"{what} of '{a.title or a.name}' and '{b.title or b.name}' are "
                                     f"{abs(ea - eb):g} px apart - align them", [a.name, b.name])
        if vshare > 0:
            gap = (b.x - a.right) if b.x >= a.right else (a.x - b.right)
            between = any(o is not a and o is not b and _overlap_1d(o.y, o.bottom, max(a.y, b.y), min(a.bottom, b.bottom)) > 0
                          and min(a.right, b.right) <= o.x and o.right <= max(a.x, b.x) for o in content)
            if 0 <= gap <= 48 and not between:
                hgaps.append((gap, a, b))
        if hshare > 0:
            gap = (b.y - a.bottom) if b.y >= a.bottom else (a.y - b.bottom)
            between = any(o is not a and o is not b and _overlap_1d(o.x, o.right, max(a.x, b.x), min(a.right, b.right)) > 0
                          and min(a.bottom, b.bottom) <= o.y and o.bottom <= max(a.y, b.y) for o in content)
            if 0 <= gap <= 48 and not between:
                vgaps.append((gap, a, b))
    gaps = [g for g, _, _ in hgaps + vgaps]
    if gaps:
        common = Counter(gaps).most_common(1)[0][0]
        for g in sorted(set(gaps) - {common}):
            if g >= 8:
                add("gutters", f"gutter of {g:g} px where the page standard is {common:g} px")
    for g, a, b in hgaps + vgaps:
        if g < 8:
            add("tight_gap", f"only {g:g} px between '{a.title or a.name}' and '{b.title or b.name}'", [a.name, b.name])

    # ---- KPI tiles
    cards = [i for i in content if i.vtype in _CARDS]
    rows = {}
    for c in cards:
        rows.setdefault(round(c.y / 8), []).append(c)
    for row in rows.values():
        if len(row) < 2:
            continue
        widths = Counter(c.w for c in row)
        if len({c.h for c in row}) > 1 or (len(widths) > 1 and not (len(widths) == 2 and min(widths.values()) == 1
                                                                    and max(widths) == [w for w, n in widths.items() if n == 1][0])):
            add("kpi_sizes", "KPI tiles in the same row have different sizes", [c.name for c in row])

    # ---- content
    if len(data) > 12:
        add("too_many", f"{len(data)} data visuals on one page - split it (8 is the house limit)")
    elif len(data) > 8:
        add("clutter", f"{len(data)} data visuals - 8 is the house limit; move detail to drillthrough",
            times=len(data) - 8)
    pies = [i for i in data if i.vtype in ("pieChart", "donutChart", "gauge")]
    for p in pies:
        add("pie", f"{p.vtype} '{p.title or p.name}': only for 5 or fewer parts; a sorted bar reads faster", [p.name])
    if len(pies) > 2:
        add("pie_overuse", f"{len(pies)} pie/donut/gauge visuals - use bars or cards for most of them")
    for i in data:
        if i.vtype == "tableEx":
            n = len((((i.doc.get("visual") or {}).get("query") or {}).get("queryState") or {}).get("Values", {}).get("projections", []))
            if n > 8:
                add("wide_table", f"table '{i.title or i.name}' has {n} columns - keep landing-page tables to 8", [i.name])
        kind = _KIND.get(i.vtype)
        if kind in SHAPE:
            _, minw, minh = SHAPE[kind]
            if i.w < 0.8 * minw or i.h < 0.8 * minh:
                add("small", f"{i.vtype} '{i.title or i.name}' is {i.w:g}x{i.h:g} - too small to read "
                             f"(aim for {minw}x{minh})", [i.name])
        if i.vtype not in _CARDS and not i.title:
            add("missing_title", f"{i.vtype} '{i.name}' has no title - say what it shows (measure and grain)", [i.name])
    for i in data + [s for s in content if s.slicer]:
        if not i.alt:
            add("missing_alt", f"{i.vtype} '{i.title or i.name}' has no alt text", [i.name])
    charts = [i for i in data if i.vtype not in _CARDS]
    if cards and charts and min(c.y for c in cards) > min(c.y for c in charts):
        add("hierarchy", "KPI tiles sit below the charts - put the headline numbers at the top")
    if not any(i.vtype == "textbox" and i.y < 120 for i in content):
        add("no_page_title", "no page title - add a title textbox at the top left")

    # ---- reading order
    ordered = [i for i in content if isinstance(i.tab, (int, float))]
    if len(ordered) > 1:
        by_tab = [i.name for i in sorted(ordered, key=lambda i: i.tab)]
        by_read = [i.name for i in sorted(ordered, key=lambda i: (round(i.y / 24), i.x))]
        if by_tab != by_read:
            add("tab_order", "tab order does not follow reading order (top-left to bottom-right)")

    # ---- density
    usable = (W - 32) * (H - 16)
    cover = sum(i.w * i.h for i in content) / usable if usable else 0
    if cover > 0.97:
        add("cramped", f"visuals cover {cover:.0%} of the canvas - leave gutters")
    elif cover < 0.35 and content:
        add("sparse", f"visuals cover only {cover:.0%} of the canvas")

    # ---- style
    blob = json.dumps([((i.doc.get("visual") or {}).get("objects"), (i.doc.get("visual") or {}).get("visualContainerObjects"))
                       for i in content if i.vtype != "textbox"])
    hexes = set(_HEX.findall(blob))
    if len(hexes) > 3:
        add("hardcoded_colours", f"{len(hexes)} hard-coded colours in visual formatting - format through the theme")
    for i in content:
        if i.vtype == "textbox":
            for para in (((i.doc.get("visual") or {}).get("objects") or {}).get("general", [{}])[0]
                         .get("properties", {}).get("paragraphs", []) or []):
                for run in para.get("textRuns", []):
                    m = re.match(r"([\d.]+)pt", str((run.get("textStyle") or {}).get("fontSize", "")))
                    if m and float(m.group(1)) < 9:
                        add("tiny_text", f"text '{run.get('value', '')[:30]}' is {m.group(1)}pt - use 9pt or more", [i.name])
    if tokens is not None:
        from .themes import theme_report
        rep = theme_report(tokens)
        for c in rep["checks"]:
            if not c["ok"]:
                add("theme_contrast", f"theme {c['check']}: {c['ratio']}:1, needs {c['needs']}:1")
        for w in rep["palette"]["warnings"]:
            add("theme_palette", f"theme palette: {w}")

    # ---- score
    penalty, spent = 0, Counter()
    for f in found:
        _, pts, cap = RULES[f["rule"]]
        take = min(pts * f.get("times", 1), cap - spent[f["rule"]])
        spent[f["rule"]] += max(take, 0)
        penalty += max(take, 0)
    score = max(0, 100 - penalty)
    order = {"error": 0, "warn": 1, "info": 2}
    found.sort(key=lambda f: order[f["severity"]])
    return {"score": score, "grade": grade(score), "issues": found,
            "errors": sum(f["severity"] == "error" for f in found),
            "stats": {"visuals": len(items), "data_visuals": len(data), "slicers": sum(i.slicer for i in content),
                      "coverage": round(cover, 2), "width": W, "height": H}}


def grade(score: float) -> str:
    return "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"


def load_page(page_dir) -> tuple:
    page_dir = Path(page_dir)
    page = json.loads((page_dir / "page.json").read_text(encoding="utf-8-sig"))
    visuals = []
    for vj in sorted(page_dir.glob("visuals/*/visual.json")):
        try:
            visuals.append(json.loads(vj.read_text(encoding="utf-8-sig")))
        except json.JSONDecodeError:
            visuals.append({"name": vj.parent.name, "visual": {"visualType": "invalid-json"}})
    return page, visuals


def lint_report(report_dir, tokens=None, only_page: Optional[str] = None) -> dict:
    """Lint every page of a PBIR report plus cross-page consistency."""
    pages_dir = Path(report_dir) / "definition" / "pages"
    if not (pages_dir / "pages.json").exists():
        raise FileNotFoundError(f"{report_dir} is not a PBIR report (no definition/pages/pages.json)")
    order = json.loads((pages_dir / "pages.json").read_text(encoding="utf-8-sig")).get("pageOrder", [])
    dirs = sorted((d for d in pages_dir.iterdir() if (d / "page.json").exists()),
                  key=lambda d: order.index(d.name) if d.name in order else 999)
    results, titles, sizes = [], {}, set()
    for d in dirs:
        page, visuals = load_page(d)
        if only_page and only_page not in (d.name, page.get("displayName")):
            continue
        if page.get("type") == "Tooltip" or str(page.get("pageBinding", {}).get("type", "")) == "Tooltip":
            continue
        r = lint_page(page, visuals, tokens)
        results.append({"name": d.name, "displayName": page.get("displayName", d.name), **r})
        sizes.add((page.get("width"), page.get("height")))
        for i in items_from(visuals):
            if i.vtype == "textbox" and i.y < 120:
                titles[d.name] = (i.x, i.y)
                break
    cross = []
    if len(sizes) > 1:
        cross.append({"rule": "page_sizes", "severity": "warn", "message": f"pages use {len(sizes)} different sizes"})
    if len(set(titles.values())) > 1:
        cross.append({"rule": "title_position", "severity": "warn",
                      "message": "page titles sit in different places on different pages - keep one position"})
    score = round(sum(r["score"] for r in results) / len(results)) - 3 * len(cross) if results else 0
    return {"pages": results, "cross_page": cross, "score": max(0, score), "grade": grade(score)}


def summary(result: dict, limit: int = 8) -> str:
    """Compact text for tool results and the CLI."""
    lines = [f"Design score {result['score']}/100 (grade {result['grade']})"]
    for f in result.get("issues", [])[:limit]:
        lines.append(f"  [{f['severity']}] {f['message']}")
    more = len(result.get("issues", [])) - limit
    if more > 0:
        lines.append(f"  ... {more} more")
    return "\n".join(lines)
