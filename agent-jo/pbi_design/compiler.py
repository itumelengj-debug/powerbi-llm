"""
Dashboard compiler: a short, high-level spec -> complete PBIR page and visual files.

The model decides WHAT the page should say (KPIs, charts, fields, titles); this compiler decides HOW it
looks: archetype, grid placement, shape-aware slot assignment, titles, alt text, reading order,
sorting, slicer modes and theme-driven formatting. Field names are checked against the semantic model
before anything is produced.

Spec (JSON):
{
  "title": "FX Desk Overview",
  "subtitle": "Notional and activity, current year",            optional
  "layout": "auto | executive | operational | analytical | detail",
  "kpis": ["_Measures[Notional (ZAR)]", {"measure": "[Trade Count]", "label": "Trades"},
           {"measure": "[Notional (ZAR)]", "label": "Notional vs PY", "trend": "'Date'[Month]",
            "target": "[Notional PY]"}],                       trend/target -> native KPI visual
  "visuals": [
    {"type": "line", "title": "Notional by month", "category": "'Date'[Month]", "values": ["[Notional (ZAR)]"]},
    {"type": "bar",  "title": "Top clients", "category": "'Client'[Client Name]", "values": "[Notional (ZAR)]"},
    {"type": "table", "columns": ["'Client'[Client Name]", "[Notional (ZAR)]"]}
  ],
  "slicers": ["'Date'[Year]", {"field": "'Client'[Segment]", "mode": "Basic"}]
}
Visual keys by type: category, values, series, lines (combo), columns (table), rows (matrix), x, y, size,
details (scatter), target, min, max (gauge, kpi), breakdown (waterfall), explain_by (decomposition),
sort ("desc" | "asc" | "none"), labels (true/false), primary (true), alt (alt text override).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import layout as L
from . import pbir as P
from .model import FieldError, Model, resolve

SLICER_MODES = {"dropdown": "Dropdown", "basic": "Basic", "list": "Basic", "between": "Between",
                "single": "Single", "before": "Before", "after": "After", "relative": "Relative"}
SPEC_KEYS = {"title", "subtitle", "layout", "kpis", "visuals", "slicers", "page_name", "theme", "notes"}
VISUAL_KEYS = {"type", "title", "category", "values", "series", "lines", "columns", "rows", "x", "y", "size",
               "details", "target", "min", "max", "breakdown", "explain_by", "sort", "labels", "primary", "alt"}
MAX_DATA_VISUALS = 10


@dataclass
class Compiled:
    page_name: str = ""
    display_name: str = ""
    archetype: str = ""
    files: dict = field(default_factory=dict)          # "pages/<page>/..." -> document
    placements: list = field(default_factory=list)     # [{name, role, type, title, x, y, width, height}]
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    unverified: list = field(default_factory=list)     # field refs that could not be checked (no model)
    orphans: list = field(default_factory=list)        # visuals from an earlier build no longer in the spec
    lint: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.files)

    def visual_docs(self) -> list:
        return [d for k, d in self.files.items() if k.endswith("/visual.json")]

    def page_doc(self) -> dict:
        return next((d for k, d in self.files.items() if k.endswith("/page.json")), {})


def _as_list(v) -> list:
    if v is None or v == "":
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _label(f) -> str:
    return f.name


def _validate(spec) -> tuple:
    errors, warnings = [], []
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"], []
    for k in spec:
        if k not in SPEC_KEYS:
            warnings.append(f"ignored unknown spec key '{k}'")
    title = spec.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("spec.title is required (the page title)")
    elif len(title) > 80:
        errors.append("spec.title is longer than 80 characters - shorten it")
    lay = str(spec.get("layout") or "auto").lower()
    if lay != "auto" and lay not in L.ARCHETYPES:
        errors.append(f"spec.layout '{lay}' is not one of auto, {', '.join(L.ARCHETYPES)}")
    kpis, vis, sl = _as_list(spec.get("kpis")), _as_list(spec.get("visuals")), _as_list(spec.get("slicers"))
    if not kpis and not vis:
        errors.append("the spec has no kpis and no visuals")
    if len(kpis) > 6:
        errors.append(f"{len(kpis)} KPI tiles: at most 6 per page - keep the ones that drive decisions")
    if len(vis) > 8:
        errors.append(f"{len(vis)} visuals: at most 8 in the main area - split into pages or use drillthrough")
    if len(kpis) + len(vis) > MAX_DATA_VISUALS:
        errors.append(f"{len(kpis) + len(vis)} data visuals: at most {MAX_DATA_VISUALS} per page")
    if len(sl) > 6:
        errors.append(f"{len(sl)} slicers: at most 6 - use a filter pane or a slicer panel page")
    for i, v in enumerate(vis, 1):
        if not isinstance(v, dict):
            errors.append(f"visuals[{i}] must be an object")
            continue
        t = str(v.get("type") or "").lower()
        if t not in P.VISUALS:
            errors.append(f"visuals[{i}].type '{v.get('type')}' is not supported - use one of: {', '.join(sorted(P.VISUALS))}")
            continue
        for key in P.REQUIRED[t]:
            if not _as_list(v.get(key)):
                errors.append(f"visuals[{i}] ({t}) needs '{key}'")
        if t == "scatter" and not (_as_list(v.get("details")) or _as_list(v.get("series"))):
            errors.append(f"visuals[{i}] (scatter) needs 'details' or 'series' (the points)")
        for k in v:
            if k not in VISUAL_KEYS:
                warnings.append(f"visuals[{i}]: ignored unknown key '{k}'")
            elif k not in ("type", "title", "sort", "labels", "primary", "alt") and k not in P.VISUALS[t][1]:
                warnings.append(f"visuals[{i}] ({t}): '{k}' does not apply to this type and was ignored")
    return errors, warnings


def compile_dashboard(spec: dict, model: Optional[Model] = None, versions: Optional[dict] = None,
                      existing: Optional[dict] = None, tokens=None, page_name: Optional[str] = None) -> Compiled:
    """Compile a dashboard spec. `existing` is pbir.existing_names(report) for collision and orphan checks."""
    out = Compiled()
    out.errors, out.warnings = _validate(spec)
    if out.errors:
        return out
    versions = versions or dict(P.DEFAULT_VERSIONS)
    existing = existing or {}
    kpis = _as_list(spec.get("kpis"))
    vis = [dict(v, type=str(v["type"]).lower()) for v in _as_list(spec.get("visuals"))]
    slicers = _as_list(spec.get("slicers"))
    kinds = [v["type"] for v in vis]

    def res(ref, default_kind, where):
        try:
            f = resolve(ref, model, default_kind)
        except FieldError as e:
            out.errors.append(f"{where}: {e}")
            return None
        if not f.verified and f.dax not in out.unverified:
            out.unverified.append(f.dax)
        return f

    # ---- fields
    kpi_fields = []
    for i, k in enumerate(kpis, 1):
        ref = k.get("measure") if isinstance(k, dict) else k
        f = res(ref, "Measure", f"kpis[{i}]")
        if f is not None and f.kind != "Measure":
            out.warnings.append(f"kpis[{i}]: {f.dax} is a column - KPI tiles should show a measure")
        trend = target = None
        if isinstance(k, dict) and k.get("trend"):
            trend = res(k["trend"], "Column", f"kpis[{i}].trend")
        if isinstance(k, dict) and k.get("target"):
            target = res(k["target"], "Measure", f"kpis[{i}].target")
            if not k.get("trend"):
                out.errors.append(f"kpis[{i}]: a target needs a 'trend' axis column too (the KPI visual shows both)")
        kpi_fields.append((f, (k.get("label") if isinstance(k, dict) else None), trend, target))
    vis_roles = []
    for i, v in enumerate(vis, 1):
        roles = {}
        for key in P.VISUALS[v["type"]][1]:
            refs = _as_list(v.get(key))
            default = "Measure" if key in P.MEASURE_KEYS else "Column"
            if v["type"] == "table":
                default = "Column"
            roles[key] = [f for f in (res(r, default, f"visuals[{i}].{key}") for r in refs) if f is not None]
        vis_roles.append(roles)
    slicer_fields = []
    for i, s in enumerate(slicers, 1):
        ref = s.get("field") if isinstance(s, dict) else s
        f = res(ref, "Column", f"slicers[{i}]")
        mode_in = str((s.get("mode") if isinstance(s, dict) else "") or "dropdown").lower()
        if mode_in not in SLICER_MODES:
            out.errors.append(f"slicers[{i}]: mode '{mode_in}' - use one of {', '.join(sorted(set(SLICER_MODES.values())))}")
        if f is not None and f.kind == "Measure":
            out.errors.append(f"slicers[{i}]: {f.dax} is a measure - slicers need a column")
        slicer_fields.append((f, SLICER_MODES.get(mode_in, "Dropdown"), s.get("title") if isinstance(s, dict) else None))
    if out.errors:
        return out

    # ---- layout
    lay = str(spec.get("layout") or "auto").lower()
    archetype = L.choose_archetype(len(kpis), kinds, len(slicers)) if lay == "auto" else lay
    if archetype != "analytical" and len(slicers) > 4:
        out.errors.append(f"{len(slicers)} slicers do not fit the header band of a '{archetype}' page - "
                          "use layout 'analytical' (slicer rail) or at most 4 slicers")
        return out
    primary = next((i for i, v in enumerate(vis) if v.get("primary")), None)
    if primary is None:
        primary = next((i for i, k in enumerate(kinds) if k in ("table", "matrix")), 0) if archetype == "detail" else 0
    heights = [176 if m == "Basic" else 80 for _, m, _ in slicer_fields]
    try:
        page = L.layout_page(archetype, len(kpis), kinds, len(slicers), primary=primary,
                             slicer_heights=heights if archetype == "analytical" else None)
    except ValueError as e:
        out.errors.append(str(e))
        return out

    # ---- names
    title = spec["title"].strip()
    pname = page_name or spec.get("page_name") or P.name_for("page", title)
    if pname in existing and existing[pname].get("displayName") not in ("", title):
        pname = P.name_for("page", title, "2")
    out.page_name, out.display_name, out.archetype = pname, title, archetype

    # ---- documents (z / tabOrder assigned after, in reading order)
    vc, pv = versions.get("visualContainer", "2.9.0"), versions.get("page", "2.0.0")
    items = []
    muted = getattr(tokens, "muted", "") if tokens is not None else ""
    text_c = getattr(tokens, "text", "") if tokens is not None else ""
    items.append(("title", "textbox", page.title, lambda z, t, n: P.textbox_doc(
        n, page.title, z, t, vc, title, spec.get("subtitle") or "", text_c, muted), title))
    for i, ((f, mode, hdr), box) in enumerate(zip(slicer_fields, page.slicers)):
        items.append((f"slicer{i}", "slicer", box, (lambda f=f, mode=mode, hdr=hdr, box=box:
                      lambda z, t, n: P.slicer_doc(n, box, z, t, vc, f, mode, hdr))(), hdr or f.name))
    for i, ((f, label, trend, target), box) in enumerate(zip(kpi_fields, page.kpis)):
        lab = label or _label(f)
        if model is not None and f.verified and not f.format_string:
            out.warnings.append(f"{f.dax} has no format string - set one (e.g. #,0 or #,0,,\"M\" for millions) so the tile reads cleanly")
        if trend is not None:
            def mk(z, t, n, f=f, lab=lab, box=box, trend=trend, target=target):
                roles = {"values": [f], "category": [trend], "target": [target] if target else []}
                return P.visual_doc(n, "kpi", roles, box, z, t, vc, lab,
                                    f"KPI: {lab} with trend by {trend.name}" + (f" against {target.name}" if target else ""),
                                    sort=(trend, "Ascending"))
            items.append((f"kpi{i}", "kpi", box, mk, lab))
            continue

        def mk(z, t, n, f=f, lab=lab, box=box):
            return P.visual_doc(n, "card", {"values": [f]}, box, z, t, vc, lab, f"KPI tile: {lab}",
                                objects={"categoryLabels": [{"properties": {"show": P.lit(False)}}]})
        items.append((f"kpi{i}", "card", box, mk, lab))
    for i, (v, roles, box) in enumerate(zip(vis, vis_roles, page.main)):
        kind = v["type"]
        vals = roles.get("values") or roles.get("y") or []
        cat = (roles.get("category") or roles.get("rows") or roles.get("details") or roles.get("explain_by") or [None])[0]
        auto_title = (" and ".join(f.name for f in vals[:2]) + (f" by {cat.name}" if cat else "")) if vals else \
            ", ".join(f.name for f in (roles.get("columns") or [])[:3])
        vtitle = (v.get("title") or auto_title or P.TYPE_LABEL[kind]).strip()
        alt = v.get("alt") or (f"{P.TYPE_LABEL[kind]} of " + (", ".join(f.name for f in vals) or vtitle)
                               + (f" by {cat.name}" if cat else ""))
        sort, objects = None, {}
        order = str(v.get("sort") or "").lower()
        if kind in ("bar", "column", "stackedbar", "stackedcolumn", "funnel") and cat is not None and vals:
            if order == "asc" or (not order and cat.timelike):
                sort = (cat, "Ascending")
            elif order != "none":
                sort = (vals[0], "Descending")      # rankings read best sorted, largest first
        elif kind in ("line", "area", "stackedarea", "combo", "ribbon", "waterfall") and cat is not None:
            if order != "none":
                sort = (cat, "Ascending")
        if "labels" in v:
            objects["labels"] = [{"properties": {"show": P.lit(bool(v["labels"]))}}]
        if kind in ("donut", "pie"):
            out.warnings.append(f"visuals[{i + 1}]: {kind} charts only read well with 5 or fewer slices - "
                                "prefer a sorted bar chart if the category has more")

        def mk(z, t, n, kind=kind, roles=roles, box=box, vtitle=vtitle, alt=alt, sort=sort, objects=objects):
            return P.visual_doc(n, kind, roles, box, z, t, vc, vtitle, alt, sort, objects or None)
        items.append((f"visual{i}", kind, box, mk, vtitle))

    reading = sorted(range(len(items)), key=lambda k: (items[k][2].y, items[k][2].x))
    taken = set()
    for other, info in existing.items():
        if other != pname:
            taken |= info.get("visuals", set())
    built = set()
    out.files[f"pages/{pname}/page.json"] = P.page_doc(pname, title, pv)
    for rank, k in enumerate(reading, 1):
        role, kind, box, make, label = items[k]
        vname = P.name_for(pname, role, kind)
        while vname in taken or vname in built:
            vname = P.name_for(pname, role, kind, vname)
        built.add(vname)
        doc = make(rank * 1000, rank * 1000, vname)
        out.files[f"pages/{pname}/visuals/{vname}/visual.json"] = doc
        out.placements.append({"name": vname, "role": role, "type": kind, "title": label, **box.as_dict()})
    if pname in existing:
        out.orphans = sorted(existing[pname].get("visuals", set()) - built)
    if out.unverified:
        out.warnings.append("not checked against a model (no semantic model found): " + ", ".join(out.unverified))

    from .lint import lint_page          # local import: lint uses pbir helpers too
    out.lint = lint_page(out.page_doc(), out.visual_docs(), tokens=tokens)
    return out
