---
updated: 2026-11-06
---
# Colour and accessibility

## Contrast thresholds
Text needs at least 4.5:1 contrast against its background (WCAG 2.x AA for normal text); large text 3:1.
Chart marks, axis lines that carry meaning and icons need 3:1 against the visual background (WCAG 1.4.11
non-text contrast). Light greys used for "context" series often fail 3:1 on white; darken them slightly.

## Colour-vision deficiency
Roughly 1 in 12 men and 1 in 200 women have a red-green colour-vision deficiency. Test palettes under
simulated protanopia and deuteranopia: blue and purple, or red, orange and green, can collapse into the
same hue. Never encode meaning by colour alone - add a sign, an arrow, a label or a shape. The Okabe-Ito
palette (blue, vermillion, bluish green, reddish purple, orange, sky blue) is designed to stay
distinguishable.

## Using colour with intent
- Grey for context, one accent colour for the thing the reader should notice.
- Semantic colours (good, bad, neutral) only for signed variance or status, consistently across pages.
- Sequential palettes (light to dark) for ordered values, diverging palettes for values around a midpoint.
- Six to eight categorical colours at most; group the rest.
- Same field, same colour on every page: set it once in the theme or in the visual's data colours.

## Dark themes
Use a near-black navy rather than pure black, lift chart colours to their lighter shades (Tailwind-style
400 tones) to keep 3:1, and keep secondary text above 4.5:1 - dark themes fail on grey labels more often
than on data colours.

## Accessibility checklist
Alt text on every data visual (Format > General > Alt text), a logical tab order (View > Selection >
Tab order), titles that say what the visual shows, no information in colour alone, minimum 9 pt text,
and a high-contrast-safe theme.
