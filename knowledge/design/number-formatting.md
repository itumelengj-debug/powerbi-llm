---
updated: 2026-11-06
---
# Number formatting

## Format in the model
Set formatString on every measure (TMDL formatString, or Measure tools > Format) so cards, tables and
tooltips agree; avoid per-visual overrides. Common strings: "#,0" (whole numbers with thousands
separators), "#,0.00" (two decimals), "0.0%" (percent), "0.0000" (FX rates).

## Scaling large values
In Power BI format strings, a thousands separator immediately to the left of the decimal position scales
the value by 1,000 per comma: "##0,," shows 100,000,000 as 100, and "#,0,,\"M\"" adds an M suffix. For
decimals on scaled values, prefer the visual's Display units setting (Thousands, Millions, Billions) with
Value decimal places, or a dynamic format string, and check the result in Desktop. Unverified: decimal
and scaling-comma combinations behave differently between tools; test before relying on one.

## Dynamic format strings
A measure can carry a dynamic format string (a DAX expression returning the format), for example to
switch between K, M and bn by magnitude or to follow a currency selected in a slicer. It changes only
the display, so the value stays numeric for sorting and totals - better than FORMAT(), which turns the
measure into text.

## Locale
Format strings follow the report or model culture: with en-ZA the decimal separator is a comma. Inside
DAX that builds text for machines (SVG coordinates, URLs, JSON), use FORMAT(value, "0.0", "en-US") so
the separator is always a point.
