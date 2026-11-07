---
updated: 2026-11-06
---
# Dashboard layout and grid

## The house grid
Canvas 1280 x 720 (16:9), display option Fit to page. Every x, y, width and height is a multiple of 8.
Outer margins 24 px, gutters between visuals exactly 16 px. The 1232 px content width divides exactly into
halves (608), thirds (400), quarters (296) and sixths (192), so rows built from these units line up with
each other: a 2:1 split is two thirds plus a gutter (816) and one third (400), and its edges meet the
edges of a row of three KPI tiles above it. Edges that are almost aligned (a few pixels off) look like
mistakes; align them exactly or offset them deliberately by a full grid unit.

## Page archetypes
- Executive: title and up to 4 dropdown slicers in a top band (y 16, height 80); KPI row at y 112
  (height 120); primary visual large on the left (two thirds), supporting visuals on the right.
- Operational: same band; compact KPI row (height 104) for up to 6 tiles; an even grid of charts.
- Analytical: title band (height 48), a slicer rail on the left (width 176), KPIs and charts to the right.
- Detail: band, optional KPIs, a short chart strip, then a full-width table or matrix.
Dashboard Studio (design_dashboard) builds all four from a spec; hand-placed pages should follow the
same coordinates.

## Reading order and hierarchy
People scan a page top-left to bottom-right (a Z pattern). Put the headline numbers top-left, the main
trend or comparison in the largest slot, and detail at the bottom or behind drillthrough. The 3-30-300
rule: the overview answers its question in 3 seconds, filters and interaction take 30 seconds, detail
takes up to 300. Tab order (and z order) should follow the same reading order for keyboard and screen
reader users.

## Density
One question per page. About 8 data visuals is the practical ceiling for a landing page: beyond that
every visual gets too small and each visual sends its own queries, so the page also gets slower. Move
the rest to drillthrough pages, tooltip pages or a second page. Give tables and matrices width (a
full-width row), rankings height (a tall column) and donuts or gauges a square slot.

## Consistency across pages
Keep the title, slicers and navigation in the same place on every page, use the same page size, and
sync slicers that filter the same field (View > Sync slicers). Readers learn the layout once.
