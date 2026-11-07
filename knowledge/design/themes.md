---
updated: 2026-11-06
---
# Report themes

## Why theme-first formatting
Formatting in the theme (View > Themes > Browse for themes) styles every visual at once, keeps pages
consistent, and keeps visual.json files small. Per-visual colour and font overrides drift apart and make
re-branding a manual job. Dashboard Studio puts titles, backgrounds, borders, padding, axes, table styles
and slicer styles in the theme and leaves visuals with only their query, title and alt text.

## Theme structure
- name, dataColors (the categorical palette in order), foreground, background, tableAccent, good,
  neutral, bad, maximum, center, minimum.
- textClasses: callout (KPI values), title, header, label - each with fontFace, fontSize and color.
- visualStyles: formatting by visual type and object, with plain values (no expr wrappers).
  "*": {"*": {...}} applies to every visual; "card": {"*": {...}} overrides for cards; "page" styles the
  canvas background.

## Presets in Dashboard Studio
boardroom (light, blue accent), midnight (dark), fxdesk (light finance), okabe-ito (colour-blind safe)
and mono (greys with one accent for storytelling), or a theme derived from one brand colour. Every theme
is checked for text contrast (4.5:1), chart colour contrast (3:1) and palette separability under
simulated colour-vision deficiency before it is staged.
