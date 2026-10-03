# PassionCode brand, vendored

The dashboard uses the PassionCode design system 1.1.0. `passioncode-tokens.css` is
the canonical `design-system/tokens.css` byte for byte; `manifest.json` records the
source commit and SHA-256 of every vendored file, and `tests/test_i18n.py` fails
when the bytes drift or when the dark control edge (`--pc-border-strong`) falls under
3:1 on any surface. The file's opt-in light palette (`:root[data-theme="light"]`, new
in 1.1.0) is not used: every dashboard page declares `data-theme="dark"`. The Mac app reads the
same dark values (`macos/Sources/ObservatoryCore/Palette.swift`, held to this file by
`PaletteTests`), and `site/tokens.css` carries the same bytes between its markers.
Do not edit these files here: change the canonical source, review the rendered site
and the dashboard, then copy the bytes and update the pin (all three copies).

`observatory-mark.svg` is the product glyph: an observing lens with a gold point on
the dark PassionCode tile, drawn with the same tile, border and stroke weight as the
Switchboard mark. The dashboard inlines it as the favicon and the rail's mark.

The dashboard's own role names (`--bg`, `--accent`, …) are aliases of the semantic
`--pc-*` roles, declared in `build_dashboard.py`'s template. No rule names a colour.
