---
updated: 2026-11-06
---
# KPI tiles

## Anatomy of a good tile
A tile answers "how are we doing?" in one glance: a short label, the value, and context - a comparison
(vs target, vs prior period) and ideally a trend. A number without context cannot be judged. Keep tiles
the same size in a row (or one deliberate wider "hero" tile), at most six per page, in the order the
business reads them.

## Native options
- Card (card): value plus label. Dashboard Studio hides the category label and uses the visual title as
  the label so the value stands alone and large.
- KPI visual (kpi): Indicator (value), Trend axis (a date column) and Target (goal); it shows the value,
  the distance to goal with colour, and the trend behind it. Use it when the trend matters.
- Multi-row card: several related values in one tile, for compact summaries.
- New card visual (cardVisual): several values with reference labels and images; its formatting uses
  selectors, so clone a Desktop-saved exemplar before styling it in PBIR.

## Variance done right
Show the comparison as a signed percentage with an arrow, not colour alone: "▲ 4.2% vs PY". Use the
theme's good and bad colours and remember that red and green converge for people with deuteranopia, so
the arrow or sign must carry the meaning. An SVG delta badge measure (svg_measure kind delta_badge) gives
a pill with arrow and colour that works in tables and cards.

## Formatting the value
Format in the measure's format string, not per visual, so every tile and table agrees. Large values need
scaling (thousands, millions): use the visual's Display units setting or a scaling format string (see
the number formatting guide). Keep decimals consistent across tiles.
