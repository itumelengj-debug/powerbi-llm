"""
SVG preview of a PBIR page: real positions, titles, visual types and theme colours, with sample chart
shapes (not your data) so a layout can be judged before Power BI Desktop is opened - by you in a
browser, or by the model's vision input. Standard library only.
"""
from __future__ import annotations

import hashlib
import html
import math
import random

from .lint import items_from
from .themes import Tokens

FONT = "Segoe UI, Segoe UI Web, -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif"
NOTE = "Layout preview - sample shapes, not your data"


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _rng(name: str) -> random.Random:
    return random.Random(int(hashlib.sha1(name.encode("utf-8")).hexdigest()[:8], 16))


def _series(r: random.Random, n: int, lo: float = 0.25, hi: float = 0.95) -> list:
    v, out = r.uniform(0.35, 0.6), []
    trend = r.uniform(-0.01, 0.03)
    for i in range(n):
        v = min(hi, max(lo, v + trend + r.uniform(-0.09, 0.09) + 0.05 * math.sin(i / 1.7)))
        out.append(v)
    return out


_PCT = ("%", "margin", "pct", "percent", "share", "sla")
_DEC = ("rate", "ratio", "avg", "average", "mean", "price", "usd", "eur", "gbp", "score", "index")
_INT = ("count", "client", "customer", "order", "ticket", "trade", "user", "people", "headcount", "escalation",
        "backlog", "open", "visits", "deals", "#", "number of")


def _sample_value(label: str, r: random.Random) -> str:
    """A plausible-looking sample for a KPI tile, guessed from its label (previews only, never data)."""
    low = label.lower()
    if any(k in low for k in _PCT):
        return f"{r.uniform(2, 96):.1f}%"
    if any(k in low for k in _DEC):
        return f"{r.uniform(0.5, 25):.2f}"
    if any(k in low for k in _INT):
        return f"{r.randint(120, 9800):,}"
    return f"{r.uniform(1.2, 98):.1f}{r.choice(['K', 'M', 'bn'])}"


def _text_runs(doc: dict) -> list:
    paras = (((doc.get("visual") or {}).get("objects") or {}).get("general", [{}])[0]
             .get("properties", {}).get("paragraphs", [])) or []
    runs = []
    for p in paras:
        for r in p.get("textRuns", [])[:1]:
            st = r.get("textStyle") or {}
            try:
                pt = float(str(st.get("fontSize", "11pt")).replace("pt", ""))
            except ValueError:
                pt = 11.0
            runs.append((r.get("value", ""), pt, st.get("fontWeight") == "bold", st.get("color")))
    return runs


def _proj_names(doc: dict, role: str) -> list:
    qs = (((doc.get("visual") or {}).get("query") or {}).get("queryState") or {})
    return [p.get("nativeQueryRef") or p.get("queryRef", "") for p in (qs.get(role) or {}).get("projections", [])]


def render_page_svg(page: dict, visual_docs: list, tokens: Tokens, note: str = NOTE) -> str:
    W, H = int(page.get("width") or 1280), int(page.get("height") or 720)
    t = tokens
    pal = list(t.data) or ["#2563EB"]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
           f'font-family="{_e(FONT)}" role="img" aria-label="{_e(page.get("displayName", "Report page"))}">',
           f'<rect width="{W}" height="{H}" fill="{t.page}"/>']
    items = sorted(items_from(visual_docs), key=lambda i: (i.doc.get("position") or {}).get("z", 0))
    for it in items:
        x, y, w, h = it.x, it.y, it.w, it.h
        r = _rng(it.name)
        vt = it.vtype
        if vt == "textbox":
            cy = y + 4
            for k, (txt, pt, bold, col) in enumerate(_text_runs(it.doc)):
                px = pt * 4 / 3
                cy += px + (2 if k == 0 else 6)
                out.append(f'<text x="{x}" y="{cy:.0f}" font-size="{px:.0f}" font-weight="{600 if bold else 400}" '
                           f'fill="{_e(col or (t.text if k == 0 else t.muted))}">{_e(txt)}</text>')
            continue
        if it.decorative:
            out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{t.radius}" fill="{t.surface_alt}" opacity="0.6"/>')
            continue
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{t.radius}" fill="{t.surface}" '
                   f'stroke="{t.border}" stroke-width="1"/>')
        pad = 12
        if vt == "slicer":
            header = (((it.doc.get("visual") or {}).get("objects") or {}).get("header") or [{}])[0]
            label = it.title or _proj_names(it.doc, "Values")[:1] or ["Slicer"]
            hdr = header.get("properties", {}).get("text", {}).get("expr", {}).get("Literal", {}).get("Value", "")
            label = hdr.strip("'") if hdr else (label[0] if isinstance(label, list) else label)
            out.append(f'<text x="{x + pad}" y="{y + 24}" font-size="12" font-weight="600" fill="{t.muted}">{_e(label)}</text>')
            by = y + h - 40 if h < 120 else y + 36
            out.append(f'<rect x="{x + pad}" y="{by}" width="{w - 2 * pad}" height="28" rx="4" fill="{t.surface}" stroke="{t.border}"/>')
            out.append(f'<text x="{x + pad + 10}" y="{by + 19}" font-size="12" fill="{t.text}">All</text>')
            cx = x + w - pad - 16
            out.append(f'<path d="M{cx - 4} {by + 12} l4 4 l4 -4" fill="none" stroke="{t.muted}" stroke-width="1.5"/>')
            continue
        title_h = 0
        if it.title and vt not in ("card", "multiRowCard", "kpi", "cardVisual"):
            out.append(f'<text x="{x + pad}" y="{y + 24}" font-size="13" font-weight="600" fill="{t.text}">{_e(it.title)}</text>')
            title_h = 28
        cx, cy, cw, ch = x + pad, y + pad + title_h, w - 2 * pad, h - 2 * pad - title_h
        if cw <= 8 or ch <= 8:
            continue
        if vt in ("card", "multiRowCard", "kpi", "cardVisual"):
            label = it.title or (_proj_names(it.doc, "Values") or _proj_names(it.doc, "Indicator") or ["KPI"])[0]
            out.append(f'<text x="{x + pad + 4}" y="{y + 28}" font-size="12" font-weight="600" fill="{t.muted}">{_e(label)}</text>')
            val = _sample_value(str(label), r)
            size = max(18, min(34, h * 0.32))
            if vt == "kpi":      # native KPI visual: value top-left, trend area behind, goal and variance
                pts = _series(r, 12)
                top = y + 36
                coords = [(x + 8 + k * (w - 16) / 11, y + h - 8 - v * (h - 44)) for k, v in enumerate(pts)]
                d = " ".join(f"{px:.1f},{py:.1f}" for px, py in coords)
                out.append(f'<polygon points="{x + 8},{y + h - 8} {d} {x + w - 8},{y + h - 8}" fill="{t.good}" opacity="0.12"/>')
                out.append(f'<text x="{x + pad + 4}" y="{top + size * 0.8:.0f}" font-size="{size:.0f}" font-weight="600" fill="{t.text}">{val}</text>')
                delta = r.uniform(-9, 14)
                col = t.good if delta >= 0 else t.bad
                out.append(f'<text x="{x + pad + 4}" y="{top + size * 0.8 + 18:.0f}" font-size="11" fill="{col}">'
                           f'{"+" if delta >= 0 else "-"}{abs(delta):.1f}% vs goal</text>')
                continue
            # classic card: value centred in the tile, as Desktop draws it
            out.append(f'<text x="{x + w / 2:.0f}" y="{y + 18 + (h - 18) / 2 + size * 0.36:.0f}" font-size="{size:.0f}" '
                       f'font-weight="600" text-anchor="middle" fill="{t.text}">{val}</text>')
            continue
        base = cy + ch
        if vt in ("lineChart", "areaChart", "stackedAreaChart", "lineClusteredColumnComboChart",
                  "lineStackedColumnComboChart"):
            for g in range(1, 4):
                gy = cy + ch * g / 4
                out.append(f'<line x1="{cx}" y1="{gy:.0f}" x2="{cx + cw}" y2="{gy:.0f}" stroke="{t.gridline}" stroke-dasharray="2 3"/>')
            n = 12
            if vt.startswith("lineClustered") or vt.startswith("lineStacked"):
                bw = cw / n * 0.6
                for i, v in enumerate(_series(r, n, 0.2, 0.7)):
                    bx = cx + i * cw / n + (cw / n - bw) / 2
                    out.append(f'<rect x="{bx:.1f}" y="{base - v * ch:.1f}" width="{bw:.1f}" height="{v * ch:.1f}" rx="2" fill="{pal[0]}"/>')
                series = [(pal[1 % len(pal)], _series(r, n, 0.45, 0.95))]
            else:
                series = [(pal[k % len(pal)], _series(r, n)) for k in range(1 if len(_proj_names(it.doc, "Y")) < 2 else 2)]
            for col, vals in series:
                pts = [(cx + i * cw / (n - 1), base - v * ch) for i, v in enumerate(vals)]
                d = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
                if vt in ("areaChart", "stackedAreaChart"):
                    out.append(f'<polygon points="{cx},{base} {d} {cx + cw},{base}" fill="{col}" opacity="0.15"/>')
                out.append(f'<polyline points="{d}" fill="none" stroke="{col}" stroke-width="2.5" stroke-linejoin="round"/>')
        elif vt in ("clusteredColumnChart", "columnChart", "hundredPercentStackedColumnChart", "ribbonChart"):
            n = 12
            bw = cw / n * 0.62
            stacked = vt != "clusteredColumnChart"
            for i, v in enumerate(_series(r, n, 0.2, 0.95)):
                bx = cx + i * cw / n + (cw / n - bw) / 2
                if stacked:
                    parts = [v * 0.5, v * 0.3, v * 0.2]
                    top = base
                    for k, pv in enumerate(parts):
                        out.append(f'<rect x="{bx:.1f}" y="{top - pv * ch:.1f}" width="{bw:.1f}" height="{pv * ch:.1f}" fill="{pal[k % len(pal)]}"/>')
                        top -= pv * ch
                else:
                    out.append(f'<rect x="{bx:.1f}" y="{base - v * ch:.1f}" width="{bw:.1f}" height="{v * ch:.1f}" rx="2" fill="{pal[0]}"/>')
        elif vt in ("clusteredBarChart", "barChart", "hundredPercentStackedBarChart", "funnel"):
            n = max(4, min(8, int(ch // 30)))
            vals = sorted((r.uniform(0.25, 1.0) for _ in range(n)), reverse=True)
            lab_w = min(110, cw * 0.28)
            bh = ch / n * 0.62
            for i, v in enumerate(vals):
                by = cy + i * ch / n + (ch / n - bh) / 2
                if vt == "funnel":
                    bw = (cw - 20) * v
                    out.append(f'<rect x="{cx + (cw - bw) / 2:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="2" fill="{pal[0]}"/>')
                    continue
                out.append(f'<rect x="{cx}" y="{by + bh * 0.3:.1f}" width="{lab_w * r.uniform(0.5, 0.9):.1f}" height="{bh * 0.4:.1f}" rx="2" fill="{t.border}"/>')
                bw = (cw - lab_w - 44) * v
                out.append(f'<rect x="{cx + lab_w + 8:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="2" fill="{pal[0]}"/>')
                out.append(f'<text x="{cx + lab_w + 14 + bw:.1f}" y="{by + bh * 0.72:.1f}" font-size="10" fill="{t.muted}">{v * 99:.0f}</text>')
        elif vt in ("donutChart", "pieChart"):
            rad = min(cw, ch) / 2 - 4
            ox, oy = cx + cw / 2, cy + ch / 2
            parts = [0.42, 0.26, 0.18, 0.14]
            a0 = -math.pi / 2
            for k, pv in enumerate(parts):
                a1 = a0 + pv * 2 * math.pi
                large = 1 if pv > 0.5 else 0
                x0, y0 = ox + rad * math.cos(a0), oy + rad * math.sin(a0)
                x1, y1 = ox + rad * math.cos(a1), oy + rad * math.sin(a1)
                out.append(f'<path d="M{ox:.1f},{oy:.1f} L{x0:.1f},{y0:.1f} A{rad:.1f},{rad:.1f} 0 {large} 1 {x1:.1f},{y1:.1f} Z" '
                           f'fill="{pal[k % len(pal)]}" stroke="{t.surface}" stroke-width="2"/>')
                a0 = a1
            if vt == "donutChart":
                out.append(f'<circle cx="{ox:.1f}" cy="{oy:.1f}" r="{rad * 0.6:.1f}" fill="{t.surface}"/>')
        elif vt == "gauge":
            rad = min(cw / 2, ch) - 6
            ox, oy = cx + cw / 2, cy + ch - 4
            out.append(f'<path d="M{ox - rad:.1f},{oy:.1f} A{rad:.1f},{rad:.1f} 0 0 1 {ox + rad:.1f},{oy:.1f}" fill="none" '
                       f'stroke="{t.border}" stroke-width="{rad * 0.25:.1f}"/>')
            ang = math.pi * (1 - r.uniform(0.45, 0.85))
            out.append(f'<path d="M{ox - rad:.1f},{oy:.1f} A{rad:.1f},{rad:.1f} 0 0 1 {ox + rad * math.cos(ang):.1f},{oy - rad * math.sin(ang):.1f}" '
                       f'fill="none" stroke="{pal[0]}" stroke-width="{rad * 0.25:.1f}"/>')
        elif vt == "scatterChart":
            for _ in range(28):
                px, py = r.uniform(0.05, 0.95), r.uniform(0.05, 0.95)
                py = min(0.95, max(0.05, 0.85 * px + r.uniform(-0.25, 0.2)))
                out.append(f'<circle cx="{cx + px * cw:.1f}" cy="{base - py * ch:.1f}" r="{r.uniform(3, 7):.1f}" fill="{pal[0]}" opacity="0.7"/>')
        elif vt == "waterfallChart":
            n, level = 8, 0.3
            bw = cw / n * 0.6
            for i in range(n):
                step = r.uniform(-0.18, 0.25) if 0 < i < n - 1 else (0.3 if i == 0 else 0)
                lo, hi = sorted((level, level + step)) if i < n - 1 else (0, level)
                col = t.good if step >= 0 else t.bad
                if i in (0, n - 1):
                    col = pal[0]
                bx = cx + i * cw / n + (cw / n - bw) / 2
                out.append(f'<rect x="{bx:.1f}" y="{base - hi * ch:.1f}" width="{bw:.1f}" height="{max(2, (hi - lo) * ch):.1f}" fill="{col}"/>')
                level = max(0.05, min(0.95, level + step)) if i < n - 1 else level
        elif vt in ("tableEx", "pivotTable"):
            cols = max(2, min(6, len(_proj_names(it.doc, "Values")) + len(_proj_names(it.doc, "Rows")) or 3))
            rows = max(3, int(ch // 26))
            rh = ch / (rows + 1)
            out.append(f'<line x1="{cx}" y1="{cy + rh:.1f}" x2="{cx + cw}" y2="{cy + rh:.1f}" stroke="{t.border}"/>')
            for c in range(cols):
                hx = cx + c * cw / cols
                out.append(f'<rect x="{hx + 4:.1f}" y="{cy + rh * 0.35:.1f}" width="{cw / cols * 0.5:.1f}" height="{rh * 0.3:.1f}" rx="2" fill="{t.muted}" opacity="0.55"/>')
            for k in range(rows):
                ry = cy + rh * (k + 1)
                if k % 2:
                    out.append(f'<rect x="{cx}" y="{ry:.1f}" width="{cw}" height="{rh:.1f}" fill="{t.surface_alt}"/>')
                for c in range(cols):
                    hx = cx + c * cw / cols
                    wv = cw / cols * (r.uniform(0.25, 0.6) if c else r.uniform(0.45, 0.8))
                    tx = hx + 4 if c == 0 else hx + cw / cols - wv - 8
                    out.append(f'<rect x="{tx:.1f}" y="{ry + rh * 0.38:.1f}" width="{wv:.1f}" height="{rh * 0.26:.1f}" rx="2" '
                               f'fill="{t.text if (vt == "pivotTable" and c == 0) else t.muted}" opacity="{0.8 if c == 0 else 0.45}"/>')
        elif vt == "decompositionTreeVisual":
            levels = 3
            for lv in range(levels):
                nodes = 1 if lv == 0 else 3
                for k in range(nodes):
                    nx = cx + lv * cw / levels
                    ny = cy + (k + 0.5) * ch / nodes - 14
                    out.append(f'<rect x="{nx + 6:.1f}" y="{ny:.1f}" width="{cw / levels - 30:.1f}" height="28" rx="4" fill="{pal[0]}" opacity="{1 - 0.25 * lv}"/>')
        else:
            out.append(f'<rect x="{cx}" y="{cy}" width="{cw}" height="{ch}" fill="none" stroke="{t.border}" stroke-dasharray="4 4"/>')
            out.append(f'<text x="{cx + cw / 2:.0f}" y="{cy + ch / 2:.0f}" font-size="12" text-anchor="middle" fill="{t.muted}">{_e(vt)}</text>')
    if note:
        out.append(f'<text x="{W - 24}" y="{H - 8}" font-size="10" text-anchor="end" fill="{t.muted}">{_e(note)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def render_html(svg: str, title: str, lint: dict = None, tokens: Tokens = None) -> str:
    bg = tokens.page if tokens else "#F4F6FA"
    fg = tokens.text if tokens else "#0F172A"
    rows = ""
    if lint:
        items = "".join(f"<li><b>{_e(i['severity'])}</b> {_e(i['message'])}</li>" for i in lint.get("issues", [])[:12])
        rows = (f"<p>Design score <b>{lint.get('score')}</b>/100 (grade {_e(lint.get('grade'))})</p>"
                + (f"<ul>{items}</ul>" if items else "<p>No issues found.</p>"))
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width, initial-scale=1'><title>{_e(title)}</title><style>"
            f"body{{margin:0;padding:24px;background:{bg};color:{fg};font-family:{FONT}}}"
            f"svg{{max-width:100%;height:auto;box-shadow:0 1px 3px rgba(0,0,0,.12);border-radius:8px}}"
            f"ul{{line-height:1.6}}</style></head><body><h1 style='font-size:20px'>{_e(title)}</h1>{svg}{rows}</body></html>")
