"""
SVG measures: DAX measures that return an SVG image, for visuals Power BI does not draw natively
(sparklines in a table row, progress and bullet bars, variance badges) - the technique behind most
premium-looking Power BI reports.

How they work
  The measure returns "data:image/svg+xml;utf8,<svg ...>". Set the measure's data category to Image URL
  (TMDL: dataCategory: ImageUrl) and put it in a table, matrix or the new card visual; size it with the
  visual's image height/width settings.
Rules every generator here follows
  - '#' is written as %23 and '%' as %25: the string is a URI, where '#' starts a fragment.
  - Numbers in the SVG go through FORMAT(..., "0.0", "en-US"): a comma decimal separator (en-ZA, de-DE...)
    would corrupt the coordinates.
  - Blank in, blank out: no image when the inputs are blank, so totals and empty rows stay clean.
Test any of them with run_dax_query:  EVALUATE ROW("svg", <measure expression>)
"""
from __future__ import annotations

import re

from . import color as C

KINDS = ("sparkline", "progress", "bullet", "delta_badge", "colour_rule")


def _measure_ref(ref: str) -> str:
    """'_Measures[Notional (ZAR)]' or '[Notional (ZAR)]' -> '[Notional (ZAR)]'."""
    m = re.search(r"\[([^\]]+)\]\s*$", ref or "")
    if not m:
        raise ValueError(f"'{ref}' is not a measure reference - write [Measure] or Table[Measure]")
    return f"[{m.group(1)}]"


def _column_ref(ref: str) -> str:
    """'Date'[Date] / Date[Date] -> 'Date'[Date]."""
    m = re.match(r"^\s*(?:'((?:[^']|'')+)'|([^\[\]']+?))\s*\[([^\]]+)\]\s*$", ref or "")
    if not m:
        raise ValueError(f"'{ref}' is not a column reference - write 'Table'[Column]")
    table = m.group(1) or m.group(2)
    return f"'{table}'[{m.group(3)}]"


def _uri_hex(c: str) -> str:
    return "%23" + C.norm_hex(c)[1:]


def sparkline(measure: str, axis: str, colour: str = "#2563EB", width: int = 120, height: int = 32,
              marker: bool = True) -> str:
    """Line of `measure` over the values of date column `axis` in the current filter context
    (e.g. every month of the selected year for each client row)."""
    m, ax = _measure_ref(measure), _column_ref(axis)
    col = _uri_hex(colour)
    dot = (" & \"<circle cx='\" & _LastX & \"' cy='\" & _LastY & \"' r='2.5' fill='" + col + "'/>\"") if marker else ""
    return f"""VAR _Points =
    FILTER (
        ADDCOLUMNS ( VALUES ( {ax} ), "@x", {ax}, "@y", {m} ),
        NOT ISBLANK ( [@y] )
    )
VAR _XMin = MINX ( _Points, [@x] )
VAR _XMax = MAXX ( _Points, [@x] )
VAR _YMin = MINX ( _Points, [@y] )
VAR _YMax = MAXX ( _Points, [@y] )
VAR _XSpan = DATEDIFF ( _XMin, _XMax, DAY )
VAR _Line =
    CONCATENATEX (
        _Points,
        FORMAT ( DIVIDE ( DATEDIFF ( _XMin, [@x], DAY ), _XSpan ) * 100, "0.0", "en-US" ) & ","
            & FORMAT ( 28 - DIVIDE ( [@y] - _YMin, _YMax - _YMin, 0.5 ) * 26, "0.0", "en-US" ),
        " ",
        [@x], ASC
    )
VAR _LastX = "100"
VAR _LastY =
    FORMAT ( 28 - DIVIDE ( MAXX ( TOPN ( 1, _Points, [@x], DESC ), [@y] ) - _YMin, _YMax - _YMin, 0.5 ) * 26, "0.0", "en-US" )
RETURN
    IF (
        COUNTROWS ( _Points ) >= 2,
        "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
            & "viewBox='-3 -1 106 32' preserveAspectRatio='none'>"
            & "<polyline fill='none' stroke='{col}' stroke-width='2' stroke-linejoin='round' "
            & "stroke-linecap='round' vector-effect='non-scaling-stroke' points='" & _Line & "'/>"{dot}
            & "</svg>"
    )"""


def progress(value: str, target: str, colour: str = "#2563EB", track: str = "#E2E8F0",
             width: int = 120, height: int = 12) -> str:
    """Bar filled to value / target (capped at 100%), with the percentage left to the visual's own label."""
    v, t = _measure_ref(value), _measure_ref(target)
    return f"""VAR _Value = {v}
VAR _Target = {t}
VAR _Pct = MIN ( MAX ( DIVIDE ( _Value, _Target ), 0 ), 1 )
VAR _W = FORMAT ( _Pct * 100, "0.0", "en-US" )
RETURN
    IF (
        NOT ISBLANK ( _Value ) && NOT ISBLANK ( _Target ) && _Target <> 0,
        "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
            & "viewBox='0 0 100 10' preserveAspectRatio='none'>"
            & "<rect x='0' y='2' width='100' height='6' rx='3' fill='{_uri_hex(track)}'/>"
            & "<rect x='0' y='2' width='" & _W & "' height='6' rx='3' fill='{_uri_hex(colour)}'/>"
            & "</svg>"
    )"""


def bullet(value: str, target: str, colour: str = "#0F172A", band: str = "#E2E8F0", target_colour: str = "#B91C1C",
           width: int = 140, height: int = 20) -> str:
    """Stephen Few bullet: value bar against a target tick on a qualitative band; scale is 0..max(value, target) x 1.15."""
    v, t = _measure_ref(value), _measure_ref(target)
    return f"""VAR _Value = {v}
VAR _Target = {t}
VAR _Max = MAX ( _Value, _Target ) * 1.15
VAR _V = FORMAT ( MAX ( DIVIDE ( _Value, _Max ), 0 ) * 100, "0.0", "en-US" )
VAR _T = FORMAT ( MAX ( DIVIDE ( _Target, _Max ), 0 ) * 100, "0.0", "en-US" )
RETURN
    IF (
        NOT ISBLANK ( _Value ) && NOT ISBLANK ( _Target ) && _Max > 0,
        "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
            & "viewBox='0 0 100 20' preserveAspectRatio='none'>"
            & "<rect x='0' y='2' width='100' height='16' fill='{_uri_hex(band)}'/>"
            & "<rect x='0' y='7' width='" & _V & "' height='6' fill='{_uri_hex(colour)}'/>"
            & "<rect x='" & _T & "' y='3' width='1.5' height='14' fill='{_uri_hex(target_colour)}' transform='translate(-0.75 0)'/>"
            & "</svg>"
    )"""


def delta_badge(actual: str, comparison: str, good: str = "#047857", bad: str = "#B91C1C",
                good_bg: str = "#D1FAE5", bad_bg: str = "#FEE2E2", higher_is_better: bool = True) -> str:
    """Pill with an arrow and the % change: colour AND shape carry the meaning (never colour alone)."""
    a, c = _measure_ref(actual), _measure_ref(comparison)
    up_good = "_Delta >= 0" if higher_is_better else "_Delta <= 0"
    return f"""VAR _Actual = {a}
VAR _Comparison = {c}
VAR _Delta = DIVIDE ( _Actual - _Comparison, ABS ( _Comparison ) )
VAR _IsGood = {up_good}
VAR _Fg = IF ( _IsGood, "{_uri_hex(good)}", "{_uri_hex(bad)}" )
VAR _Bg = IF ( _IsGood, "{_uri_hex(good_bg)}", "{_uri_hex(bad_bg)}" )
VAR _Arrow = IF ( _Delta >= 0, UNICHAR ( 9650 ), UNICHAR ( 9660 ) )
VAR _Text = SUBSTITUTE ( _Arrow & " " & FORMAT ( ABS ( _Delta ), "0.0%", "en-US" ), "%", "%25" )
RETURN
    IF (
        NOT ISBLANK ( _Delta ),
        "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='76' height='22' viewBox='0 0 76 22'>"
            & "<rect width='76' height='22' rx='11' fill='" & _Bg & "'/>"
            & "<text x='38' y='15' font-family='Segoe UI, sans-serif' font-size='11' font-weight='600' "
            & "text-anchor='middle' fill='" & _Fg & "'>" & _Text & "</text></svg>"
    )"""


def colour_rule(measure: str, good: str = "#047857", bad: str = "#B91C1C", neutral: str = "",
                threshold: float = 0.0, higher_is_better: bool = True) -> str:
    """Hex colour for conditional formatting (Format > Font/Background colour > Field value). Not an image."""
    m = _measure_ref(measure)
    cmp_good = ">" if higher_is_better else "<"
    neutral_line = f'\n        _Value = {threshold:g}, "{C.norm_hex(neutral)}",' if neutral else ""
    return f"""VAR _Value = {m}
RETURN
    SWITCH (
        TRUE (),
        ISBLANK ( _Value ), BLANK (),{neutral_line}
        _Value {cmp_good} {threshold:g}, "{C.norm_hex(good)}",
        "{C.norm_hex(bad)}"
    )"""


def generate(kind: str, **kw) -> str:
    fns = {"sparkline": sparkline, "progress": progress, "bullet": bullet, "delta_badge": delta_badge,
           "colour_rule": colour_rule}
    if kind not in fns:
        raise ValueError(f"unknown SVG measure kind '{kind}' - choose from {', '.join(KINDS)}")
    return fns[kind](**kw)


def tmdl_script(table: str, name: str, expression: str, display_folder: str = "Visuals",
                image: bool = True, description: str = "") -> str:
    """createOrReplace script for TMDL view: ref table + measure, tab-indented (rulebook 8.6)."""
    tq = table if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) else "'" + table.replace("'", "''") + "'"
    nq = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else "'" + name.replace("'", "''") + "'"
    lines = ["createOrReplace", "", f"\tref table {tq}", ""]
    if description:
        lines.append(f"\t\t/// {description}")
    lines.append(f"\t\tmeasure {nq} =")
    lines += ["\t\t\t\t" + ln if ln.strip() else "" for ln in expression.splitlines()]
    if image:
        lines.append("\t\t\tdataCategory: ImageUrl")
    lines.append(f"\t\t\tdisplayFolder: {display_folder}")
    return "\n".join(lines) + "\n"


def check_dax(expr: str) -> list:
    """Cheap structural checks used by the tests and the agent before staging: balanced brackets and
    quotes, no raw '#' inside an SVG data URI."""
    problems = []
    stripped = re.sub(r'"(?:[^"]|"")*"', '""', expr)
    if stripped.count("(") != stripped.count(")"):
        problems.append("unbalanced parentheses")
    if expr.count('"') % 2:
        problems.append("unbalanced double quotes")
    if "data:image/svg" in expr and re.search(r"(fill|stroke|color)\s*=\s*'#", expr):
        problems.append("raw '#' in an SVG data URI - write %23")
    return problems
