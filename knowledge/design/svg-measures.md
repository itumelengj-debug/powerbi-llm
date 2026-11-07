---
updated: 2026-11-06
---
# SVG measures (sparklines, bars and badges)

## The technique
A measure that returns text starting with data:image/svg+xml;utf8, followed by SVG markup is drawn as
an image when its data category is Image URL (Measure tools > Data category > Image URL; in TMDL
dataCategory: ImageUrl). Put it in a table or matrix and size it in the visual's image size settings.
This is how reports show per-row sparklines, progress bars, bullet charts and variance badges that the
native visuals cannot draw.

## Rules that prevent broken images
- Write # as %23 and % as %25: the text is a URI, where # starts a fragment.
- Build coordinates with FORMAT(value, "0.0", "en-US"); a comma decimal separator (en-ZA) corrupts SVG
  numbers.
- Return BLANK() when inputs are blank so totals and empty rows show nothing.
- Use single quotes for SVG attributes inside the DAX string.
- Keep the shape simple: a few hundred points at most per image; every row evaluates the measure.

## Building blocks
- Sparkline: points from ADDCOLUMNS(VALUES(date column), ...), scaled to a 100 x 30 viewBox,
  joined with CONCATENATEX ordered by date, drawn as a polyline.
- Progress bar: a track rect and a filled rect whose width is value / target, capped at 100%.
- Bullet chart: a band, a value bar and a target tick on a shared scale.
- Delta badge: rounded rect, arrow (UNICHAR 9650 / 9660) and signed percentage, coloured good or bad.
The svg_measure tool generates and stages all of these with these rules applied; test one with
run_dax_query: EVALUATE ROW("img", [Measure]).

## Native alternative
Tables and matrices also have built-in sparklines (add a sparkline to a value in the field well). Use them
when the default line style is enough; use SVG measures when you need bars, targets, badges or styling.
