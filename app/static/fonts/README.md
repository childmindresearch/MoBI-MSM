# Embedded interface font

`ui.woff2` is **Inter v4.1** (variable, weights 100-900), vendored here because
Section 12 of the UX spec pins the interface to exactly one locally-served
webfont. Sizes in `app/static/styles.css` are expressed in degrees of visual
angle and resolved from the rig geometry in `app/config.py`, but the column
measure is in `ch` units and so depends on this font's character metrics.
Replacing the font changes line wrapping and AOI boundaries.

| | |
|---|---|
| Source | https://github.com/rsms/inter/releases/tag/v4.1 (`web/InterVariable.woff2`) |
| Licence | SIL Open Font License 1.1 — see `LICENSE-Inter.txt` |
| Size | 352,240 bytes |

Chosen for a large x-height at screen sizes and unambiguous `0`/`O` and `1`/`l`,
which matters for a task built around reading and 0-100 numeric entry.

## Replacing it

Drop a different `.woff2` in as `ui.woff2` and restart. Nothing in the code
changes — but re-check the achieved line length, because a font with different
metrics moves line wrap points and AOI boundaries. The degree-based sizes
themselves are unaffected; they come from the rig config.
