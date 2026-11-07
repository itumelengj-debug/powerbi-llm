"""
Theme engine: design tokens -> a complete Power BI report theme (View > Themes > Browse for themes).

The design engine formats through the theme, not per visual: visual.json files stay lean (query, title,
alt text, padding) and every page shares one look that can be re-skinned in one import. The theme sets
canvas and visual backgrounds, rounded borders, title and label typography, decluttered axes, table
styling, slicer styling and the categorical palette.

Presets: boardroom (light, blue), midnight (dark), fxdesk (light finance), okabe-ito (colour-blind safe),
mono (grey + one accent for storytelling). for_brand() derives a full theme from one brand colour.
Every token set is checked by theme_report(): text 4.5:1, chart colours 3:1, palette separability
under simulated colour-vision deficiency.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

from . import color as C


@dataclass
class Tokens:
    name: str
    mode: str                  # "light" | "dark"
    page: str                  # canvas
    surface: str               # visual background
    surface_alt: str           # zebra rows, slicer items
    border: str
    gridline: str
    text: str
    muted: str
    accent: str
    data: list
    good: str
    bad: str
    neutral: str
    font: str = "Segoe UI"
    font_semibold: str = "Segoe UI Semibold"
    radius: int = 8            # rounded visual borders (px)
    padding: int = 8           # visual padding (px); matches the 8D container padding the compiler writes
    categorical: bool = True   # False = highlight palette (accent + greys), not repaired for many series
    callout_size: int = 26     # KPI value (pt)
    title_size: int = 12       # visual titles (pt)
    header_size: int = 10      # table headers, slicer headers (pt)
    label_size: int = 9        # axes, data labels, legend (pt)
    notes: list = field(default_factory=list)


PRESETS = {
    "boardroom": Tokens(
        name="Boardroom", mode="light", page="#F4F6FA", surface="#FFFFFF", surface_alt="#F8FAFC",
        border="#E2E8F0", gridline="#EDF1F6", text="#0F172A", muted="#475569", accent="#2563EB",
        data=["#2563EB", "#0F766E", "#D97706", "#7C3AED", "#DB2777", "#0E7490", "#4D7C0F", "#64748B"],
        good="#047857", bad="#B91C1C", neutral="#B45309"),
    "midnight": Tokens(
        name="Midnight", mode="dark", page="#0B1220", surface="#111A2C", surface_alt="#16213A",
        border="#22304D", gridline="#1B2740", text="#E6EAF2", muted="#A3AFC2", accent="#60A5FA",
        data=["#60A5FA", "#2DD4BF", "#FBBF24", "#A78BFA", "#F472B6", "#22D3EE", "#A3E635", "#94A3B8"],
        good="#34D399", bad="#F87171", neutral="#FBBF24"),
    "fxdesk": Tokens(
        name="FX Desk", mode="light", page="#EEF2F6", surface="#FFFFFF", surface_alt="#F6F8FB",
        border="#D8E0EA", gridline="#E8EDF3", text="#0B1F33", muted="#4A5B6E", accent="#0B6E99",
        data=["#0B6E99", "#C2410C", "#15803D", "#6D28D9", "#0E7490", "#A16207", "#BE123C", "#475569"],
        good="#15803D", bad="#B91C1C", neutral="#A16207"),
    "okabe-ito": Tokens(
        name="Okabe-Ito", mode="light", page="#F7F7F5", surface="#FFFFFF", surface_alt="#FAFAF8",
        border="#E3E3DE", gridline="#EEEEEA", text="#1A1A1A", muted="#555555", accent="#0072B2",
        data=["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#555555", "#000000"],
        good="#009E73", bad="#D55E00", neutral="#E69F00",
        notes=["Okabe-Ito hues, darkened where needed to reach 3:1 on white; safe for all common CVD types."]),
    "mono": Tokens(
        name="Mono Accent", mode="light", page="#F5F5F4", surface="#FFFFFF", surface_alt="#FAFAF9",
        border="#E7E5E4", gridline="#F0EFEE", text="#1C1917", muted="#57534E", accent="#1D4ED8",
        data=["#1D4ED8", "#78716C", "#A8A29E", "#57534E", "#93C5FD", "#44403C", "#D97706", "#292524"],
        good="#047857", bad="#B91C1C", neutral="#A16207", categorical=False,
        notes=["Grey for context, one accent for the point you want seen: highlight with the first colour. "
               "Not meant for charts with many series."]),
}


def resolve(preset: str = "boardroom", brand: str = "", mode: str = "", font: str = "", name: str = "") -> Tokens:
    """Tokens for a preset or a brand colour, made accessible (text 4.5:1, chart colours 3:1)."""
    if brand:
        t = for_brand(brand, mode or "light", font=font, name=name)
    else:
        key = (preset or "boardroom").lower()
        if key not in PRESETS:
            raise ValueError(f"unknown theme preset '{preset}' - choose from {', '.join(PRESETS)}")
        t = replace(PRESETS[key])
        if mode and mode != t.mode:
            raise ValueError(f"preset '{key}' is {t.mode}; use brand_color with mode='{mode}' for a {mode} variant")
        if font:
            t.font, t.font_semibold = font, font + (" Semibold" if font == "Segoe UI" else "")
        if name:
            t.name = name
    return make_accessible(t)


def for_brand(brand: str, mode: str = "light", font: str = "", name: str = "") -> Tokens:
    base = replace(PRESETS["boardroom" if mode != "dark" else "midnight"])
    brand = C.norm_hex(brand)
    base.name = name or f"Brand {brand}"
    base.accent = brand
    base.data = C.brand_palette(brand, base.surface, 8)
    if mode == "dark":   # tint the dark neutrals slightly toward the brand hue
        base.page = C.mix(base.page, brand, 0.04)
        base.surface = C.mix(base.surface, brand, 0.05)
    else:
        base.page = C.mix("#F5F7FA", brand, 0.03)
    if font:
        base.font, base.font_semibold = font, font + (" Semibold" if font == "Segoe UI" else "")
    return base


def make_accessible(t: Tokens) -> Tokens:
    t = replace(t, data=list(t.data), notes=list(t.notes))
    t.text = C.ensure_contrast(t.text, t.surface, C.TEXT_CONTRAST)
    t.muted = C.ensure_contrast(t.muted, t.surface, C.TEXT_CONTRAST)
    t.accent = C.ensure_contrast(t.accent, t.surface, C.GRAPHIC_CONTRAST)
    t.data = (C.separable_palette(t.data, t.surface) if t.categorical
              else C.accessible_palette(t.data, t.surface, C.GRAPHIC_CONTRAST))
    t.good, t.bad, t.neutral = (C.ensure_contrast(x, t.surface, C.GRAPHIC_CONTRAST) for x in (t.good, t.bad, t.neutral))
    return t


def _solid(c: str) -> dict:
    return {"solid": {"color": c}}


def build_theme(t: Tokens) -> dict:
    """Power BI report theme JSON (plain values, no expr wrappers)."""
    container = {
        "background": [{"show": True, "color": _solid(t.surface), "transparency": 0}],
        "border": [{"show": True, "color": _solid(t.border), "radius": t.radius}],
        "dropShadow": [{"show": False}],
        "padding": [{"top": t.padding, "bottom": t.padding, "left": t.padding, "right": t.padding}],
        "title": [{"show": True, "fontColor": _solid(t.text), "fontSize": t.title_size,
                   "fontFamily": t.font_semibold, "alignment": "left"}],
        "visualHeader": [{"show": True, "foreground": _solid(t.muted), "background": _solid(t.surface),
                          "transparency": 0}],
        "categoryAxis": [{"showAxisTitle": False, "labelColor": _solid(t.muted), "fontSize": t.label_size,
                          "gridlineShow": False}],
        "valueAxis": [{"showAxisTitle": False, "labelColor": _solid(t.muted), "fontSize": t.label_size,
                       "gridlineShow": True, "gridlineColor": _solid(t.gridline), "gridlineStyle": "dotted"}],
        "legend": [{"show": True, "position": "Top", "showTitle": False, "labelColor": _solid(t.muted),
                    "fontSize": t.label_size}],
        "labels": [{"color": _solid(t.text), "fontSize": t.label_size}],
    }
    bare = {"background": [{"show": False}], "border": [{"show": False}], "title": [{"show": False}],
            "visualHeader": [{"show": False}], "dropShadow": [{"show": False}]}
    table_common = {
        "grid": [{"gridVertical": False, "gridHorizontal": True, "gridHorizontalColor": _solid(t.border),
                  "rowPadding": 4, "outlineColor": _solid(t.border)}],
        "columnHeaders": [{"fontColor": _solid(t.muted), "backColor": _solid(t.surface), "fontFamily": t.font_semibold,
                           "fontSize": t.header_size}],
        "values": [{"fontColorPrimary": _solid(t.text), "backColorPrimary": _solid(t.surface),
                    "fontColorSecondary": _solid(t.text), "backColorSecondary": _solid(t.surface_alt),
                    "fontSize": t.header_size}],
        "total": [{"fontColor": _solid(t.text), "backColor": _solid(t.surface_alt), "fontFamily": t.font_semibold}],
    }
    return {
        "name": t.name,
        "dataColors": list(t.data),
        "foreground": t.text,
        "foregroundNeutralSecondary": t.muted,
        "foregroundNeutralTertiary": C.mix(t.muted, t.surface, 0.35),
        "background": t.surface,
        "backgroundLight": t.surface_alt,
        "backgroundNeutral": t.border,
        "tableAccent": t.accent,
        "hyperlink": t.accent,
        "visitedHyperlink": t.accent,
        "good": t.good,
        "neutral": t.neutral,
        "bad": t.bad,
        "maximum": t.accent,
        "center": C.mix(t.accent, t.surface, 0.5),
        "minimum": C.mix(t.accent, t.surface, 0.88),
        "null": t.muted,
        "textClasses": {
            "callout": {"fontSize": t.callout_size, "fontFace": t.font_semibold, "color": t.text},
            "title": {"fontSize": t.title_size, "fontFace": t.font_semibold, "color": t.text},
            "header": {"fontSize": t.header_size, "fontFace": t.font_semibold, "color": t.text},
            "label": {"fontSize": t.label_size, "fontFace": t.font, "color": t.muted},
        },
        "visualStyles": {
            "*": {"*": container},
            "page": {"*": {"background": [{"color": _solid(t.page), "transparency": 0}],
                           "outspace": [{"color": _solid(t.page), "transparency": 0}]}},
            "card": {"*": {"labels": [{"color": _solid(t.text), "fontSize": t.callout_size, "fontFamily": t.font_semibold}],
                           "categoryLabels": [{"show": False}],
                           "title": [{"show": True, "fontColor": _solid(t.muted), "fontSize": t.header_size,
                                      "fontFamily": t.font_semibold, "alignment": "left"}]}},
            "slicer": {"*": {"title": [{"show": False}],
                             "header": [{"show": True, "fontColor": _solid(t.muted), "textSize": t.header_size,
                                         "fontFamily": t.font_semibold}],
                             "items": [{"fontColor": _solid(t.text), "background": _solid(t.surface),
                                        "textSize": t.header_size}]}},
            "tableEx": {"*": table_common},
            "pivotTable": {"*": {**table_common,
                                 "rowHeaders": [{"fontColor": _solid(t.text), "backColor": _solid(t.surface),
                                                 "fontSize": t.header_size}]}},
            "clusteredBarChart": {"*": {"valueAxis": [{"show": False}], "labels": [{"show": True}]}},
            "lineChart": {"*": {"lineStyles": [{"strokeWidth": 3}]}},
            "textbox": {"*": dict(bare, padding=[{"top": 0, "bottom": 0, "left": 0, "right": 0}])},
            "shape": {"*": dict(bare)},
            "image": {"*": dict(bare)},
            "actionButton": {"*": {"title": [{"show": False}], "dropShadow": [{"show": False}]}},
        },
    }


def theme_report(t: Tokens) -> dict:
    """Accessibility and separability checks for a token set (all numbers rounded for display)."""
    checks = []

    def need(label, fg, bg, ratio):
        r = round(C.contrast(fg, bg), 2)
        checks.append({"check": label, "ratio": r, "needs": ratio, "ok": r >= ratio})
    need("text on visuals", t.text, t.surface, C.TEXT_CONTRAST)
    need("secondary text on visuals", t.muted, t.surface, C.TEXT_CONTRAST)
    need("text on canvas", t.text, t.page, C.TEXT_CONTRAST)
    need("accent on visuals", t.accent, t.surface, C.GRAPHIC_CONTRAST)
    pal = C.palette_report(t.data, t.surface, first=6 if t.categorical else 1)
    variance = {k: round(C.delta_e(C.simulate(t.good, k), C.simulate(t.bad, k)), 1) if k else round(C.delta_e(t.good, t.bad), 1)
                for k in ("", "deutan", "protan")}
    warnings = [f"{c['check']}: {c['ratio']}:1 (needs {c['needs']}:1)" for c in checks if not c["ok"]] + pal["warnings"]
    notes = list(t.notes)
    if min(variance.values()) < 10:
        notes.append("good/bad colours converge under colour-blind simulation: always pair them with a sign or "
                     "arrow, never colour alone")
    return {"name": t.name, "mode": t.mode, "checks": checks, "palette": pal, "variance_de": variance,
            "warnings": warnings, "notes": notes,
            "ok": all(c["ok"] for c in checks) and not pal["low_contrast"] and not pal["warnings"]}


def tokens_dict(t: Tokens) -> dict:
    return asdict(t)
