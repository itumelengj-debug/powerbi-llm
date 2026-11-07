"""
Colour science for report design: WCAG contrast, CIEDE2000 colour difference, colour-vision-deficiency
simulation (Machado et al. 2009, severity 1.0) and accessible categorical palettes.

Thresholds used across the design engine:
  text vs its background          >= 4.5 : 1   (WCAG 2.x AA, normal text)
  data colours vs visual background >= 3 : 1   (WCAG 1.4.11 non-text contrast)
  categorical colours, normal vision    dE00 >= 12 between any two of the first six
  categorical colours, simulated CVD    dE00 >= 5 (protan, deutan), >= 3 (tritan, far rarer)
Standard library only.
"""
from __future__ import annotations

import math
from itertools import combinations

TEXT_CONTRAST = 4.5
GRAPHIC_CONTRAST = 3.0
MIN_DE_NORMAL = 12.0
MIN_DE_CVD = {"protan": 5.0, "deutan": 5.0, "tritan": 3.0}

# Machado, Oliveira & Fernandes (2009), severity 1.0, applied to linear RGB.
_CVD = {
    "protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
    "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
    "tritan": ((1.255528, -0.076749, -0.178779), (-0.078411, 0.930809, 0.147602), (0.004733, 0.691367, 0.303900)),
}


# --------------------------------------------------------------------------- conversions

def norm_hex(h: str) -> str:
    s = str(h).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        raise ValueError(f"not a hex colour: {h!r}")
    return "#" + s.upper()


def hex_to_rgb(h: str) -> tuple:
    s = norm_hex(h)[1:]
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, int(round(c)))):02X}" for c in rgb)


def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _unlin(c: float) -> float:
    c = max(0.0, min(1.0, c))
    v = c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return v * 255.0


def luminance(h: str) -> float:
    r, g, b = (_lin(c) for c in hex_to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def rgb_to_hsl(h: str) -> tuple:
    r, g, b = (c / 255.0 for c in hex_to_rgb(h))
    mx, mn = max(r, g, b), min(r, g, b)
    light = (mx + mn) / 2
    if mx == mn:
        return 0.0, 0.0, light
    d = mx - mn
    sat = d / (2 - mx - mn) if light > 0.5 else d / (mx + mn)
    if mx == r:
        hue = ((g - b) / d) % 6
    elif mx == g:
        hue = (b - r) / d + 2
    else:
        hue = (r - g) / d + 4
    return hue * 60.0, sat, light


def hsl_to_hex(hue: float, sat: float, light: float) -> str:
    hue = hue % 360
    sat, light = max(0.0, min(1.0, sat)), max(0.0, min(1.0, light))
    c = (1 - abs(2 * light - 1)) * sat
    x = c * (1 - abs((hue / 60) % 2 - 1))
    m = light - c / 2
    seg = int(hue // 60)
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][seg % 6]
    return rgb_to_hex(((r + m) * 255, (g + m) * 255, (b + m) * 255))


def mix(a: str, b: str, t: float) -> str:
    """Blend a -> b by t (0..1) in sRGB."""
    ra, rb = hex_to_rgb(a), hex_to_rgb(b)
    return rgb_to_hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


def to_lab(h: str) -> tuple:
    r, g, b = (_lin(c) for c in hex_to_rgb(h))
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b) / 1.0
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e2000(lab1, lab2) -> float:
    """CIEDE2000 colour difference (Sharma, Wu & Dalal 2005)."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    cbar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(cbar ** 7 / (cbar ** 7 + 25 ** 7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0
    dlp, dcp = L2 - L1, c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    else:
        dh = h2p - h1p
        dhp = dh if abs(dh) <= 180 else (dh - 360 if dh > 180 else dh + 360)
    dHp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))
    lbarp, cbarp = (L1 + L2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbarp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbarp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbarp = (h1p + h2p + 360) / 2
    else:
        hbarp = (h1p + h2p - 360) / 2
    t = (1 - 0.17 * math.cos(math.radians(hbarp - 30)) + 0.24 * math.cos(math.radians(2 * hbarp))
         + 0.32 * math.cos(math.radians(3 * hbarp + 6)) - 0.20 * math.cos(math.radians(4 * hbarp - 63)))
    dtheta = 30 * math.exp(-(((hbarp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(cbarp ** 7 / (cbarp ** 7 + 25 ** 7))
    sl = 1 + 0.015 * (lbarp - 50) ** 2 / math.sqrt(20 + (lbarp - 50) ** 2)
    sc = 1 + 0.045 * cbarp
    sh = 1 + 0.015 * cbarp * t
    rt = -math.sin(math.radians(2 * dtheta)) * rc
    return math.sqrt((dlp / sl) ** 2 + (dcp / sc) ** 2 + (dHp / sh) ** 2 + rt * (dcp / sc) * (dHp / sh))


def delta_e(a: str, b: str) -> float:
    return delta_e2000(to_lab(a), to_lab(b))


def simulate(h: str, kind: str) -> str:
    """How a colour looks with protanopia, deuteranopia or tritanopia ('protan', 'deutan', 'tritan')."""
    m = _CVD[kind]
    r, g, b = (_lin(c) for c in hex_to_rgb(h))
    out = [row[0] * r + row[1] * g + row[2] * b for row in m]
    return rgb_to_hex(_unlin(c) for c in out)


# --------------------------------------------------------------------------- accessibility helpers

def is_light(h: str) -> bool:
    return luminance(h) > 0.18


def ensure_contrast(fg: str, bg: str, ratio: float = TEXT_CONTRAST) -> str:
    """Darken (on light backgrounds) or lighten (on dark ones) fg until it reaches the ratio, keeping its hue."""
    fg = norm_hex(fg)
    if contrast(fg, bg) >= ratio:
        return fg
    hue, sat, light = rgb_to_hsl(fg)
    step = -0.01 if is_light(bg) else 0.01
    for _ in range(100):
        light = max(0.0, min(1.0, light + step))
        cand = hsl_to_hex(hue, sat, light)
        if contrast(cand, bg) >= ratio:
            return cand
        if light in (0.0, 1.0):
            break
    return "#000000" if is_light(bg) else "#FFFFFF"


def accessible_palette(colors, bg: str, ratio: float = GRAPHIC_CONTRAST) -> list:
    return [ensure_contrast(c, bg, ratio) for c in colors]


def min_pairwise(colors, kind: str = "") -> tuple:
    """(smallest dE00 between any two colours, that pair), optionally after CVD simulation."""
    cols = [simulate(c, kind) if kind else norm_hex(c) for c in colors]
    best, pair = float("inf"), None
    for (i, a), (j, b) in combinations(enumerate(cols), 2):
        d = delta_e(a, b)
        if d < best:
            best, pair = d, (norm_hex(colors[i]), norm_hex(colors[j]))
    return (round(best, 1) if pair else None), pair


def palette_report(colors, bg: str, first: int = 6) -> dict:
    """Contrast of every colour against bg and separability of the first `first` colours."""
    colors = [norm_hex(c) for c in colors]
    head = colors[:first]
    rep = {"contrast": {c: round(contrast(c, bg), 2) for c in colors},
           "low_contrast": [c for c in colors if contrast(c, bg) < GRAPHIC_CONTRAST],
           "min_de": {}, "closest": {}, "warnings": []}
    for kind in ("", "protan", "deutan", "tritan"):
        d, pair = min_pairwise(head, kind)
        key = kind or "normal"
        rep["min_de"][key], rep["closest"][key] = d, pair
        limit = MIN_DE_CVD[kind] if kind else MIN_DE_NORMAL
        if pair and d < limit:
            rep["warnings"].append(f"{pair[0]} and {pair[1]} are hard to tell apart"
                                   + (f" with {kind}opia" if kind else "") + f" (dE {d})")
    for c in rep["low_contrast"]:
        rep["warnings"].append(f"{c} has {rep['contrast'][c]}:1 against {norm_hex(bg)} (needs 3:1 for chart marks)")
    return rep


# Curated chart hues: Tailwind-style 600/700 shades read well on light surfaces, 400 shades on dark ones.
CHART_HUES_LIGHT = ["#2563EB", "#0F766E", "#D97706", "#7C3AED", "#DB2777", "#0E7490", "#4D7C0F", "#C2410C",
                    "#4F46E5", "#047857", "#0369A1", "#A21CAF", "#A16207", "#DC2626"]
CHART_HUES_DARK = ["#60A5FA", "#2DD4BF", "#FBBF24", "#A78BFA", "#F472B6", "#22D3EE", "#A3E635", "#FB923C",
                   "#818CF8", "#34D399", "#38BDF8", "#E879F9", "#FACC15", "#F87171"]


def brand_palette(brand: str, bg: str = "#FFFFFF", n: int = 8) -> list:
    """An ordered categorical palette that starts with the brand colour, meets 3:1 against bg and
    maximises separation (normal vision and deuteranopia) among the first colours. Companion colours
    come from a curated set of chart hues, so the result stays clean rather than muddy."""
    brand = ensure_contrast(norm_hex(brand), bg, GRAPHIC_CONTRAST)
    hues = CHART_HUES_LIGHT if is_light(bg) else CHART_HUES_DARK
    pool = [ensure_contrast(c, bg, GRAPHIC_CONTRAST) for c in hues]
    pool = [c for c in dict.fromkeys(pool) if delta_e(c, brand) >= 15]
    hue, _, _ = rgb_to_hsl(brand)
    slate = ensure_contrast(hsl_to_hex(hue, 0.14, 0.45 if is_light(bg) else 0.65), bg, GRAPHIC_CONTRAST)
    chosen = [brand]

    def score(c):
        dn = min(delta_e(c, x) for x in chosen)
        cvd = [min(delta_e(simulate(c, k), simulate(x, k)) for x in chosen) for k in ("deutan", "protan", "tritan")]
        return min(dn, 2 * cvd[0], 2 * cvd[1], 2.5 * cvd[2])
    while len(chosen) < n - 1 and pool:
        best = max(pool, key=score)
        chosen.append(best)
        pool.remove(best)
    if slate not in chosen:
        chosen.append(slate)
    return chosen[:n]


def _separation(c: str, chosen: list) -> float:
    """Normalised separation of c from every chosen colour: >= 1.0 means distinct for normal vision
    (dE 12), deuteranopia and protanopia (dE 5) and tritanopia (dE 3)."""
    worst = float("inf")
    for x in chosen:
        worst = min(worst,
                    delta_e(c, x) / 12.0,
                    delta_e(simulate(c, "deutan"), simulate(x, "deutan")) / 5.0,
                    delta_e(simulate(c, "protan"), simulate(x, "protan")) / 5.0,
                    delta_e(simulate(c, "tritan"), simulate(x, "tritan")) / 3.0)
    return worst


def separable_palette(preferred, bg: str, n: int = 8, first: int = 6) -> list:
    """Keep the preferred colours (in order) where they stay distinguishable, including under simulated
    colour-vision deficiency, and swap in curated chart hues where they do not. Every colour meets 3:1."""
    pool = CHART_HUES_LIGHT if is_light(bg) else CHART_HUES_DARK
    cands = list(dict.fromkeys(accessible_palette(list(preferred) + pool, bg, GRAPHIC_CONTRAST)))
    chosen = [cands[0]]
    while len(chosen) < min(first, n):
        rest = [c for c in cands if c not in chosen]
        if not rest:
            break
        ok = [c for c in rest if _separation(c, chosen) >= 1.0]
        chosen.append(ok[0] if ok else max(rest, key=lambda c: _separation(c, chosen)))
    for c in accessible_palette(list(preferred), bg, GRAPHIC_CONTRAST) + cands:
        if len(chosen) >= n:
            break
        if c not in chosen:
            chosen.append(c)
    return chosen[:n]
