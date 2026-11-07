"""
Layout engine: deterministic placement on the house grid (rulebook section 8.3).

Canvas 1280 x 720, 8 px grid, 24 px margins, 16 px gutters. Every x, y, width and height is a multiple
of 8 and every gutter is exactly 16, so rows and columns line up across the page.

Archetypes
  executive    title + up to 4 slicers in a top band; KPI row (120 high); primary visual large
  operational  same band; compact KPI row (104 high) for up to 6 tiles; even grid of charts
  analytical   title band; slicer rail on the left (176 wide); KPI row and charts to the right
  detail       band and optional KPIs; a chart strip on top, the table or matrix below
Visuals are matched to slots by shape: wide slots go to trends and tables, tall slots to rankings,
square slots to donuts and gauges. The primary visual always gets the largest slot.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import permutations

GRID = 8
WIDTH, HEIGHT = 1280, 720
MARGIN, GUTTER, TOP = 24, 16, 16
SLICER_W = 176
RAIL_W = 176                   # leaves 1040 px of content: 80 + 96k, so it divides like the full width

ARCHETYPES = {
    "executive": {"band_h": 80, "kpi_h": 120, "rail": False},
    "operational": {"band_h": 80, "kpi_h": 104, "rail": False},
    "analytical": {"band_h": 48, "kpi_h": 104, "rail": True},
    "detail": {"band_h": 80, "kpi_h": 104, "rail": False},
}

_R = "rows"
_C = "cols"
_EVEN = {1: (_R, [(1, [1])]), 2: (_R, [(1, [1, 1])]), 3: (_R, [(1, [1, 1, 1])]),
         4: (_R, [(1, [1, 1]), (1, [1, 1])]), 5: (_R, [(1, [1, 1]), (1, [1, 1, 1])]),
         6: (_R, [(1, [1, 1, 1]), (1, [1, 1, 1])]), 7: (_R, [(1, [1, 1, 1]), (1, [1, 1, 1, 1])]),
         8: (_R, [(1, [1, 1, 1, 1]), (1, [1, 1, 1, 1])])}
TEMPLATES = {
    "executive": {**_EVEN, 2: (_C, [(2, [1]), (1, [1])]), 3: (_C, [(2, [1]), (1, [1, 1])]),
                  4: (_R, [(1, [2, 1]), (1, [1, 1])]), 5: (_R, [(1, [2, 1]), (1, [1, 1, 1])])},
    "operational": dict(_EVEN),
    "analytical": {**_EVEN, 2: (_R, [(1, [1]), (1, [1])]), 3: (_R, [(1, [1]), (1, [1, 1])])},
    "detail": {**_EVEN, 2: (_R, [(2, [1]), (3, [1])]), 3: (_R, [(2, [1, 1]), (3, [1])]),
               4: (_R, [(2, [1, 1, 1]), (3, [1])])},
}

WIDE = {"table", "matrix", "decomposition"}
# when a wide visual is not the primary one, give it a full-width row instead of a side slot
WIDE_TEMPLATES = {2: (_R, [(1, [1]), (1, [1])]), 3: (_R, [(1, [2, 1]), (1, [1])]),
                  4: (_R, [(1, [1, 1, 1]), (1, [1])])}

# preferred aspect ratio (w / h) and minimum readable size per visual kind
SHAPE = {
    "line": (2.4, 320, 200), "area": (2.4, 320, 200), "stackedarea": (2.4, 320, 200),
    "combo": (2.2, 360, 220), "column": (1.8, 320, 200), "stackedcolumn": (1.8, 320, 200),
    "column100": (1.8, 320, 200), "bar": (1.0, 280, 200), "stackedbar": (1.1, 280, 200),
    "bar100": (1.1, 280, 200), "ribbon": (2.0, 360, 220), "waterfall": (2.0, 360, 220),
    "table": (2.0, 400, 216), "matrix": (2.0, 440, 240), "donut": (1.2, 240, 200),
    "pie": (1.2, 240, 200), "funnel": (1.2, 280, 216), "gauge": (1.5, 200, 160),
    "scatter": (1.4, 320, 240), "decomposition": (2.2, 480, 280), "card": (2.0, 160, 96),
    "kpi": (1.8, 200, 120), "multirowcard": (1.4, 240, 160),
}


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def aspect(self) -> float:
        return self.w / self.h if self.h else 0.0

    @property
    def area(self) -> int:
        return self.w * self.h

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.w, "height": self.h}


def split(total: int, weights, gutter: int = GUTTER, grid: int = GRID) -> list:
    """Divide `total` px into len(weights) parts separated by gutters; parts are multiples of `grid`
    and sum exactly to the available space, so outer edges stay aligned.

    Integer weights split on a shared unit grid when it lands on the 8 px grid: [2, 1] is exactly two
    thirds plus a gutter and one third, so its edges line up with a row of three equal tiles above it.
    The content widths used here (1232, 1040 = 80 + 96k) divide exactly into halves, thirds, quarters
    and sixths."""
    k = len(weights)
    if all(float(w).is_integer() and w > 0 for w in weights):
        units = int(sum(weights))
        avail_u = total - (units - 1) * gutter
        if avail_u > 0 and avail_u % units == 0 and (avail_u // units) % grid == 0:
            u = avail_u // units
            return [int(w) * u + (int(w) - 1) * gutter for w in weights]
    avail = total - gutter * (k - 1)
    tw = float(sum(weights))
    raw = [avail * w / tw for w in weights]
    sizes = [int(r // grid) * grid for r in raw]
    rem = avail - sum(sizes)
    order = sorted(range(k), key=lambda i: (raw[i] - sizes[i], weights[i]), reverse=True)
    i = 0
    while rem >= grid:
        sizes[order[i % k]] += grid
        rem -= grid
        i += 1
    if rem:
        sizes[order[0]] += rem
    return sizes


def kpi_row(n: int, x: int, y: int, w: int, h: int) -> list:
    """Equal tiles; five tiles become one hero tile plus four equal ones (no uneven widths)."""
    if n <= 0:
        return []
    widths = split(w, [2, 1, 1, 1, 1] if n == 5 else [1] * n)
    out, cx = [], x
    for wd in widths:
        out.append(Box(cx, y, wd, h))
        cx += wd + GUTTER
    return out


def region_boxes(template, x: int, y: int, w: int, h: int) -> list:
    kind, spec = template
    out = []
    if kind == _R:
        heights = split(h, [r[0] for r in spec])
        cy = y
        for (_, cols), rh in zip(spec, heights):
            cx = x
            for cw in split(w, cols):
                out.append(Box(cx, cy, cw, rh))
                cx += cw + GUTTER
            cy += rh + GUTTER
    else:
        widths = split(w, [c[0] for c in spec])
        cx = x
        for (_, rows), cw in zip(spec, widths):
            cy = y
            for rh in split(h, rows):
                out.append(Box(cx, cy, cw, rh))
                cy += rh + GUTTER
            cx += cw + GUTTER
    return out


def fit(kind: str, box: Box) -> float:
    pref, minw, minh = SHAPE.get(kind, (1.6, 240, 160))
    score = -abs(math.log(max(box.aspect, 0.01) / pref))
    if box.w < minw or box.h < minh:
        score -= 2.0
    return score


def assign(kinds: list, boxes: list, primary: int = 0, pinned: tuple = ()) -> list:
    """Return the box index for each visual: each visual in `pinned` (then `primary`) takes the largest
    box still free; the rest go where their shape fits best (ties keep the spec order)."""
    n = len(kinds)
    if n == 0:
        return []
    order = sorted(range(len(boxes)), key=lambda i: (-boxes[i].area, boxes[i].y, boxes[i].x))
    fixed = {}
    for v in [p for p in pinned if p != primary] + [primary]:
        if v not in fixed:
            fixed[v] = next(b for b in order if b not in fixed.values())
    rest_v = [i for i in range(n) if i not in fixed]
    rest_b = [i for i in range(len(boxes)) if i not in fixed.values()]
    best, best_perm = None, None
    for perm in permutations(rest_b, len(rest_v)):
        s = sum(fit(kinds[v], boxes[b]) for v, b in zip(rest_v, perm))
        s -= 0.001 * sum(abs(rest_b.index(b) - k) for k, b in enumerate(perm))   # stable tie-break
        if best is None or s > best:
            best, best_perm = s, perm
    result = [0] * n
    for v, b in fixed.items():
        result[v] = b
    for v, b in zip(rest_v, best_perm or ()):
        result[v] = b
    return result


@dataclass
class PageLayout:
    archetype: str
    title: Box
    slicers: list
    kpis: list
    main: list           # boxes in visual order (after assignment)
    width: int = WIDTH
    height: int = HEIGHT


def layout_page(archetype: str, n_kpis: int, kinds: list, n_slicers: int, primary: int = 0,
                slicer_heights: list = None) -> PageLayout:
    """Place title, slicers, KPI tiles and main visuals for an archetype."""
    if archetype not in ARCHETYPES:
        raise ValueError(f"unknown layout '{archetype}' - choose from {', '.join(ARCHETYPES)}")
    a = ARCHETYPES[archetype]
    n_main = len(kinds)
    if n_main > 8:
        raise ValueError("at most 8 visuals in the main area - split the content across pages")
    if n_kpis > 6:
        raise ValueError("at most 6 KPI tiles per page")
    right = WIDTH - MARGIN
    bottom = HEIGHT - MARGIN
    band_h = a["band_h"]
    slicers = []
    if a["rail"]:
        title = Box(MARGIN, TOP, WIDTH - 2 * MARGIN, band_h)
        cx = MARGIN + RAIL_W + GUTTER
        cw = right - cx
        y = TOP + band_h + GUTTER
    else:
        if n_slicers > 4:
            raise ValueError("at most 4 slicers fit in the header band - use layout 'analytical' (slicer rail)")
        sx = right
        for _ in range(n_slicers):
            sx -= SLICER_W
            slicers.insert(0, Box(sx, TOP, SLICER_W, band_h))
            sx -= GUTTER
        title_w = (sx - MARGIN) if n_slicers else (WIDTH - 2 * MARGIN)
        title = Box(MARGIN, TOP, title_w, band_h)
        cx, cw = MARGIN, WIDTH - 2 * MARGIN
        y = TOP + band_h + GUTTER
    kpis = kpi_row(n_kpis, cx, y, cw, a["kpi_h"])
    if kpis:
        y += a["kpi_h"] + GUTTER
    main = []
    if n_main:
        template = TEMPLATES[archetype][n_main]
        pinned = ()
        wide = [i for i, k in enumerate(kinds) if k in WIDE and i != primary]
        if archetype in ("executive", "operational", "analytical") and n_main in WIDE_TEMPLATES and wide:
            template = WIDE_TEMPLATES[n_main]
            pinned = (wide[0],)       # the table takes the full-width row, the primary the next largest slot
        boxes = region_boxes(template, cx, y, cw, bottom - y)
        idx = assign(kinds, boxes, primary, pinned)
        main = [boxes[i] for i in idx]
    if a["rail"]:
        # stack slicers down the rail, snapping each bottom edge to a nearby content edge so the rail
        # shares the rhythm of the KPI row and chart rows beside it
        edges = sorted({b.bottom for b in kpis + main})
        cy = TOP + band_h + GUTTER
        for sh in (slicer_heights or [80] * n_slicers):
            bot = cy + sh
            near = [e for e in edges if 0 < abs(e - bot) <= 24 and e - cy >= 64]
            if near:
                bot = min(near, key=lambda e: abs(e - bot))
            slicers.append(Box(MARGIN, cy, RAIL_W, bot - cy))
            cy = bot + GUTTER
        if slicers and slicers[-1].bottom > bottom:
            raise ValueError("the slicers do not fit in the rail - use fewer or dropdown slicers")
    return PageLayout(archetype, title, slicers, kpis, main)


def choose_archetype(n_kpis: int, kinds: list, n_slicers: int) -> str:
    """auto layout: rail for many slicers, detail for a table-led page, operational for dense pages."""
    if n_slicers > 4:
        return "analytical"
    tables = sum(k in ("table", "matrix") for k in kinds)
    if tables == 1 and len(kinds) <= 2 and kinds[-1] in ("table", "matrix"):
        return "detail"
    if n_kpis >= 5 or len(kinds) >= 4:
        return "operational"
    return "executive"
