Exact spec for the baked assets

  Two opaque PNGs per icon — one per row background (this is the "zero runtime work" choice; a
  single transparent PNG would get black-haloed edges on the gray rows, since textual-image
  composites semi-transparent edges over its default black, not the row color):

  ┌────────────────┬───────────────────────────────────────────────────────────────────────────┐
  │                │                                   value                                   │
  ├────────────────┼───────────────────────────────────────────────────────────────────────────┤
  │ Canvas         │ 96 × 96 px, fully opaque, filled with the row bg color                    │
  ├────────────────┼───────────────────────────────────────────────────────────────────────────┤
  │ Backgrounds    │ surface #1E1E1E and shade #2B3240 → so sms-key becomes sms-key on-1E1E1E  │
  │                │ + on-2B3240 (same for sms-text, etc.)                                     │
  ├────────────────┼───────────────────────────────────────────────────────────────────────────┤
  │ Icon artwork   │ your chosen color, scaled to 57 px wide × 52 px tall, pasted centered     │
  ├────────────────┼───────────────────────────────────────────────────────────────────────────┤
  │ Resulting      │ left/right ≈ 19–20 px, top/bottom 22 px (vertical > horizontal, as you    │
  │ padding        │ wanted)                                                                   │
  └────────────────┴───────────────────────────────────────────────────────────────────────────┘

  The absolute canvas size is flexible (it gets stretched to ~54–60 px at render anyway), but keep
  it square and keep the icon at these proportions — that's what the stretch expects. If you'd
  rather work at a round number, the ratio is: square canvas, icon = 59.4% of width × 54.2% of
  height, centered. Bake at ≥64px square to avoid upscaling; 96 matches the spike.

  So: with two kinds today (key, text) you'll produce 4 files; each new kind (image/audio/video…)
  adds 2.

  Runtime, once assets are baked

  The renderer collapses to a pure lookup — no PIL ops per row, ever:

  1. At startup, load each baked PNG once into a PIL image (or pre-build its Image widget), cached
     by (kind, on_shade). Four entries today.
  2. Per row, pick by kind + zebra parity and hand the cached image to the widget. No open, decode,
     recolor, resize, or composite.
  3. textual-image still encodes sixel, but it caches the encoded sixel while the source image
     object is unchanged — so reuse the same cached image/widget across refreshes and each avatar's
     sixel is encoded once and replayed. The one thing to avoid is recreating the image on every
     reload(); keep it stable.

  Net per-render avatar cost ≈ zero. The only real work is the one-time startup load of a handful
  of tiny PNGs.

  When your assets are ready, drop them in (say assets/dark/avatars/ with a clear naming scheme)
  and I'll wire the cached lookup into the OptionList → custom-row-widget refactor, carrying over
  the accent-bar selection and zebra backgrounds. And I can still commit the header/accent tui.py
  changes whenever you'd like them landed.
