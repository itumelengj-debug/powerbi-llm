---
updated: 2026-11-06
---
# Choosing the right chart

## Match the chart to the question
- Change over time: line chart (continuous), column chart for few periods or discrete totals; area only
  for a single series or a stacked composition that must add up.
- Ranking and comparison across categories: bar chart (horizontal) sorted largest first; long category
  names stay readable on the vertical axis.
- Part-to-whole: a stacked bar or 100% stacked bar; a donut only with 5 or fewer parts, never to compare
  two donuts.
- Variance and bridges (start to end, what moved it): waterfall chart with a breakdown.
- Relationship between two measures: scatter chart with a categorical field on Details.
- Distribution: column chart over binned values (a histogram).
- Single headline number: card, or the KPI visual when a trend and a goal give it context.
- Exact values and lookup: table or matrix, sorted, with conditional formatting or SVG bars for scanning.
- Many categories to explore: decomposition tree or a matrix with drill-down.

## Anti-patterns
- Pie or donut with many slices, or several pies side by side: use a sorted bar.
- Gauges for everything: a card with a variance badge or a bullet bar uses a fraction of the space.
- Dual value axes with unrelated scales: readers infer correlations that are not there.
- Unsorted bars, alphabetical rankings, or legends far from the data.
- Line charts over unordered categories (lines imply continuity).
- More than six or seven colours in one chart: group the tail into "Other".

## Sorting and Top N
Sort rankings by the measure, descending. Time axes sort by the date or a sort-by column (Month sorted
by MonthNo). For long tails show the top 10 to 15 with a Top N visual filter and put the full list in a
drillthrough table.
