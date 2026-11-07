"""
PBIR document builders that follow the rulebook templates (section 8): literal encoding, projections
with queryRef/nativeQueryRef, container padding whenever container objects are set, alt text, sort
definitions, slicer modes, and $schema versions reused from the project.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Optional

from .model import Field

SCHEMA_BASE = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
DEFAULT_VERSIONS = {"visualContainer": "2.9.0", "page": "2.0.0"}
_SCHEMA_RX = re.compile(r"/definition/(visualContainer|page)/([\d.]+)/schema\.json$")

# friendly type -> (PBIR visualType, {spec key: query role})
VISUALS = {
    "card": ("card", {"values": "Values"}),
    "multirowcard": ("multiRowCard", {"values": "Values"}),
    "kpi": ("kpi", {"values": "Indicator", "category": "TrendLine", "target": "Goal"}),
    "line": ("lineChart", {"category": "Category", "values": "Y", "series": "Series"}),
    "area": ("areaChart", {"category": "Category", "values": "Y", "series": "Series"}),
    "stackedarea": ("stackedAreaChart", {"category": "Category", "values": "Y", "series": "Series"}),
    "column": ("clusteredColumnChart", {"category": "Category", "values": "Y", "series": "Series"}),
    "stackedcolumn": ("columnChart", {"category": "Category", "values": "Y", "series": "Series"}),
    "column100": ("hundredPercentStackedColumnChart", {"category": "Category", "values": "Y", "series": "Series"}),
    "bar": ("clusteredBarChart", {"category": "Category", "values": "Y", "series": "Series"}),
    "stackedbar": ("barChart", {"category": "Category", "values": "Y", "series": "Series"}),
    "bar100": ("hundredPercentStackedBarChart", {"category": "Category", "values": "Y", "series": "Series"}),
    "combo": ("lineClusteredColumnComboChart", {"category": "Category", "values": "Y", "lines": "Y2", "series": "Series"}),
    "ribbon": ("ribbonChart", {"category": "Category", "series": "Series", "values": "Y"}),
    "waterfall": ("waterfallChart", {"category": "Category", "breakdown": "Breakdown", "values": "Y"}),
    "donut": ("donutChart", {"category": "Category", "values": "Y"}),
    "pie": ("pieChart", {"category": "Category", "values": "Y"}),
    "funnel": ("funnel", {"category": "Category", "values": "Y"}),
    "scatter": ("scatterChart", {"details": "Details", "series": "Series", "x": "X", "y": "Y", "size": "Size"}),
    "gauge": ("gauge", {"values": "Y", "min": "MinValue", "max": "MaxValue", "target": "TargetValue"}),
    "table": ("tableEx", {"columns": "Values"}),
    "matrix": ("pivotTable", {"rows": "Rows", "columns": "Columns", "values": "Values"}),
    "decomposition": ("decompositionTreeVisual", {"values": "Analyze", "explain_by": "ExplainBy"}),
}
REQUIRED = {
    "card": ["values"], "multirowcard": ["values"], "kpi": ["values", "category"],
    "line": ["category", "values"], "area": ["category", "values"], "stackedarea": ["category", "values"],
    "column": ["category", "values"], "stackedcolumn": ["category", "values"], "column100": ["category", "values"],
    "bar": ["category", "values"], "stackedbar": ["category", "values"], "bar100": ["category", "values"],
    "combo": ["category", "values", "lines"], "ribbon": ["category", "values"], "waterfall": ["category", "values"],
    "donut": ["category", "values"], "pie": ["category", "values"], "funnel": ["category", "values"],
    "scatter": ["x", "y"], "gauge": ["values"], "table": ["columns"], "matrix": ["rows", "values"],
    "decomposition": ["values", "explain_by"],
}
# spec keys whose fields default to measures (the rest default to columns)
MEASURE_KEYS = {"values", "lines", "x", "y", "size", "target", "min", "max"}
TYPE_LABEL = {"card": "Card", "multirowcard": "Multi-row card", "kpi": "KPI", "line": "Line chart",
              "area": "Area chart", "stackedarea": "Stacked area chart", "column": "Column chart",
              "stackedcolumn": "Stacked column chart", "column100": "100% stacked column chart",
              "bar": "Bar chart", "stackedbar": "Stacked bar chart", "bar100": "100% stacked bar chart",
              "combo": "Combo chart", "ribbon": "Ribbon chart", "waterfall": "Waterfall chart",
              "donut": "Donut chart", "pie": "Pie chart", "funnel": "Funnel", "scatter": "Scatter chart",
              "gauge": "Gauge", "table": "Table", "matrix": "Matrix", "decomposition": "Decomposition tree"}
DATA_VISUAL_TYPES = {v[0] for v in VISUALS.values()}
DECORATIVE = {"textbox", "shape", "image", "actionButton", "basicShape"}


def lit(v) -> dict:
    """Python value -> PBIR literal expression (rule 8.2.10)."""
    if isinstance(v, bool):
        s = "true" if v else "false"
    elif isinstance(v, int):
        s = f"{v}L"
    elif isinstance(v, float):
        s = f"{v:g}D"
    else:
        s = "'" + str(v).replace("'", "''") + "'"
    return {"expr": {"Literal": {"Value": s}}}


def name_for(*parts) -> str:
    """A stable 20-character lowercase hex name (rule 8.2.5): rebuilding the same spec yields the same names."""
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:20]


def schema_url(kind: str, version: str) -> str:
    return f"{SCHEMA_BASE}{kind}/{version}/schema.json"


def detect_versions(report_dir) -> dict:
    """Most common $schema version per kind in an existing report (rule 8.2.6), else the template defaults."""
    found = {"visualContainer": Counter(), "page": Counter()}
    pages = Path(report_dir) / "definition" / "pages"
    if pages.exists():
        for f in list(pages.glob("*/page.json")) + list(pages.glob("*/visuals/*/visual.json")):
            try:
                url = json.loads(f.read_text(encoding="utf-8-sig")).get("$schema", "")
            except (json.JSONDecodeError, OSError, AttributeError):
                continue
            m = _SCHEMA_RX.search(str(url))
            if m:
                found[m.group(1)][m.group(2)] += 1
    return {k: (c.most_common(1)[0][0] if c else DEFAULT_VERSIONS[k]) for k, c in found.items()}


def existing_names(report_dir) -> dict:
    """{page name: {"displayName": ..., "visuals": set(names)}} for a PBIR report."""
    out = {}
    pages = Path(report_dir) / "definition" / "pages"
    for pj in sorted(pages.glob("*/page.json")) if pages.exists() else []:
        try:
            doc = json.loads(pj.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            doc = {}
        out[pj.parent.name] = {"displayName": doc.get("displayName", ""),
                               "visuals": {v.parent.name for v in pj.parent.glob("visuals/*/visual.json")}}
    return out


def field_expr(f: Field) -> dict:
    return {f.kind: {"Expression": {"SourceRef": {"Entity": f.table}}, "Property": f.name}}


def projection(f: Field, native: Optional[str] = None, active: Optional[bool] = None) -> dict:
    p = {"field": field_expr(f), "queryRef": f.query_ref, "nativeQueryRef": native or f.name}
    if active is not None:
        p["active"] = active
    return p


def padding(px: int = 8) -> list:
    v = lit(float(px))
    return [{"properties": {"top": v, "bottom": v, "left": v, "right": v}}]


def container(title: Optional[str] = None, alt: Optional[str] = None, pad: int = 8) -> dict:
    out = {}
    if title:
        out["title"] = [{"properties": {"show": lit(True), "text": lit(title)}}]
    if alt:
        out["general"] = [{"properties": {"altText": lit(alt)}}]
    if out:
        out["padding"] = padding(pad)      # rule 8.2.11: declare padding whenever container objects are set
    return out


def page_doc(name: str, display_name: str, version: str, width: int = 1280, height: int = 720) -> dict:
    return {"$schema": schema_url("page", version), "name": name, "displayName": display_name,
            "displayOption": "FitToPage", "height": height, "width": width}


def position(box, z: int, tab: int) -> dict:
    return {"x": box.x, "y": box.y, "z": z, "height": box.h, "width": box.w, "tabOrder": tab}


def textbox_doc(name: str, box, z: int, tab: int, version: str, title: str, subtitle: str = "",
                title_color: str = "", subtitle_color: str = "", font: str = "") -> dict:
    run = {"value": title, "textStyle": {"fontWeight": "bold", "fontSize": "20pt"}}
    if title_color:
        run["textStyle"]["color"] = title_color
    if font:
        run["textStyle"]["fontFamily"] = font
    paragraphs = [{"textRuns": [run]}]
    if subtitle:
        sub = {"value": subtitle, "textStyle": {"fontSize": "11pt"}}
        if subtitle_color:
            sub["textStyle"]["color"] = subtitle_color
        if font:
            sub["textStyle"]["fontFamily"] = font
        paragraphs.append({"textRuns": [sub]})
    return {"$schema": schema_url("visualContainer", version), "name": name, "position": position(box, z, tab),
            "visual": {"visualType": "textbox", "objects": {"general": [{"properties": {"paragraphs": paragraphs}}]}}}


def slicer_doc(name: str, box, z: int, tab: int, version: str, f: Field, mode: str = "Dropdown",
               header: Optional[str] = None) -> dict:
    vis = {"visualType": "slicer",
           "query": {"queryState": {"Values": {"projections": [projection(f)]}}},
           "objects": {"data": [{"properties": {"mode": lit(mode)}}],
                       "header": [{"properties": {"show": lit(True), "text": lit(header or f.name)}}]},
           "visualContainerObjects": container(alt=f"Slicer for {header or f.name}")}
    return {"$schema": schema_url("visualContainer", version), "name": name, "position": position(box, z, tab),
            "visual": vis}


def visual_doc(name: str, kind: str, roles: dict, box, z: int, tab: int, version: str, title: str,
               alt: str, sort: Optional[tuple] = None, objects: Optional[dict] = None) -> dict:
    """roles: {spec key: [Field, ...]} for keys of VISUALS[kind]; sort: (Field, 'Ascending'|'Descending')."""
    vtype, role_map = VISUALS[kind]
    qs = {}
    for key, role in role_map.items():
        fields = roles.get(key) or []
        if not fields:
            continue
        seen, projs = Counter(), []
        for i, f in enumerate(fields):
            seen[f.name] += 1
            native = f.name if seen[f.name] == 1 else f"{f.name} ({seen[f.name]})"   # rule 8.2.7: unique nativeQueryRef
            # Category always marks the level shown (as Desktop saves it); Rows only for drill hierarchies
            active = True if i == 0 and (role == "Category" or (role == "Rows" and len(fields) > 1)) else None
            projs.append(projection(f, native, active))
        qs[role] = {"projections": projs}
    query = {"queryState": qs}
    if sort:
        query["sortDefinition"] = {"sort": [{"field": field_expr(sort[0]), "direction": sort[1]}],
                                   "isDefaultSort": True}
    vis = {"visualType": vtype, "query": query}
    if objects:
        vis["objects"] = objects
    vis["visualContainerObjects"] = container(title=title, alt=alt)
    vis["drillFilterOtherVisuals"] = True
    return {"$schema": schema_url("visualContainer", version), "name": name, "position": position(box, z, tab),
            "visual": vis}
