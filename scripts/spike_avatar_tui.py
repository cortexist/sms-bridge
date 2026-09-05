#!/usr/bin/env python3
"""Sixel avatar proof — real conversation rows with icon avatars, in Textual.

Run in foot (or any sixel/kitty terminal), from the project venv so textual-image
is present:

    ./.venv/bin/python scripts/spike_avatar_tui.py

Half-block gave only 2px per cell, so a 3-line avatar was 6px of mush. This uses
textual-image's Image widget, which fills the actual cell height (~18px/line in
foot) and negotiates sixel itself — so a 3-line x 6-cell avatar is ~54px and the
line-art icons read. The point of this spike is to confirm that in the real
terminal before refactoring the production list off OptionList.

PoC icon mapping (per the plan): sms-key for a conversation whose newest inbound
message is a 2FA code, sms-text for everything else. image/audio/video/etc. come
later once the approach is proven.

Rows are custom widgets (Horizontal: avatar + text), which is exactly what the
production list will need — OptionList options can't host an image widget.
"""

import os
from pathlib import Path

from PIL import Image as PILImage
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static
from textual_image.widget import Image

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "dark"

# The two conversation-list backgrounds (resolved from the Textual dark theme):
# normal rows vs the zebra band. Selection is a left accent bar, not a background
# swap, so an icon only ever needs these two composited variants.
SURFACE = (0x1E, 0x1E, 0x1E)
SHADE = (0x2B, 0x32, 0x40)

# 100 stand-in conversations to exercise scrolling: (name, preview, is_2fa, on_shade).
# Zebra by row parity; every 3rd row is a 2FA (key), the rest text -- i%3 vs i%2
# are uncorrelated, so keys land on both backgrounds and all four avatar variants
# are exercised.
_PANGRAM = "The quick brown fox jumps over the lazy dog"
ROWS = [
    (
        f"Contact {i:03d}",
        f"{i:03d}: {_PANGRAM}",
        (i % 3 == 0),
        (i % 2 == 1),
    )
    for i in range(100)
]

AVATAR_W = 6      # cells: the avatar column width
ROW_H = 3         # lines per conversation row
# The avatar widget fills the whole 6x3 column, but the icon occupies only a
# fraction of it and the rest is background-coloured padding, baked into the
# image in PIXELS so the ratio is exact (cell-integer sizing was too coarse and
# couldn't split the vertical leftover evenly). A 6-cell x 3-line box is roughly
# square in foot, so the canvas is square; the icon keeps a wider-than-tall ratio
# (de-elongated) and is centred, so top/bottom padding (23% each) exceeds
# left/right (20.5% each). These fractions are spike knobs -- once the size is
# settled the icon set is regenerated at this geometry and this resize goes away.
ICON_W_FRAC = 0.59   # 3/4 of the earlier 0.79
ICON_H_FRAC = 0.54   # 3/4 of the earlier 0.72
CANVAS_PX = 96    # square working canvas; the widget scales it to the cell box

# Text colours pinned to fixed values, identical on both row backgrounds. The
# theme default $text is "auto 87%" -- auto-contrast re-resolved against each
# row's background -- so surface and shade rows landed on slightly different RGB
# (and [dim] preview more so, since dim blends toward the background). Pinning
# removes that divergence. NAME is the black-row text tone; the icon matches it.
NAME_COLOR = "#e2e2e2"      # auto-87% white over the #1E1E1E surface row
PREVIEW_COLOR = "#9a9a9a"

# The source PNGs are pure white line-art. Recolour to the same tone as the name
# text so the avatars sit with it instead of glaring brighter. Spike knob; the
# regenerated set bakes this in.
ICON_COLOR = (0xE2, 0xE2, 0xE2)


# One composited image per (kind, background) — shared across every row that uses
# it, so textual-image encodes each distinct avatar's sixel once instead of per
# row. This mirrors what production will do with the baked PNGs (load once, cache
# by key). With key/text x surface/shade that is four images for the whole list.
_AVATAR_CACHE: dict[tuple[bool, tuple[int, int, int]], PILImage.Image] = {}


def avatar_image(is_2fa: bool, bg: tuple[int, int, int]) -> PILImage.Image:
    key = (is_2fa, bg)
    if key not in _AVATAR_CACHE:
        _AVATAR_CACHE[key] = _build_avatar(is_2fa, bg)
    return _AVATAR_CACHE[key]


def _build_avatar(is_2fa: bool, bg: tuple[int, int, int]) -> PILImage.Image:
    """The mapped icon, scaled to ICON_W_FRAC x ICON_H_FRAC of a square canvas
    and centred on a background-coloured field. The transparent PNG is blended
    over the same background so its own gaps match; the padding field then makes
    the visible icon smaller than the avatar column, with more top/bottom space."""
    name = "sms-key-45-dt.png" if is_2fa else "sms-text-45-dt.png"
    icon = PILImage.open(ASSETS / name).convert("RGBA")

    # Recolour: keep the alpha (so anti-aliased edges still blend), replace the
    # white RGB with the text tone.
    tinted = PILImage.new("RGBA", icon.size, ICON_COLOR + (255,))
    tinted.putalpha(icon.getchannel("A"))
    icon = tinted

    iw = round(CANVAS_PX * ICON_W_FRAC)
    ih = round(CANVAS_PX * ICON_H_FRAC)
    icon = icon.resize((iw, ih), PILImage.LANCZOS)

    canvas = PILImage.new("RGBA", (CANVAS_PX, CANVAS_PX), bg + (255,))
    canvas.alpha_composite(icon, ((CANVAS_PX - iw) // 2, (CANVAS_PX - ih) // 2))
    return canvas.convert("RGB")


class Row(Horizontal):
    def __init__(self, name, preview, is_2fa, on_shade):
        super().__init__()
        self._name = name
        self._preview = preview
        self._img = avatar_image(is_2fa, SHADE if on_shade else SURFACE)
        if on_shade:
            self.add_class("shade")

    def compose(self) -> ComposeResult:
        # The widget fills the whole 6x3 avatar column; the padding is inside the
        # image (see avatar_image), so no extra container or fractional cells.
        av = Image(self._img)
        av.styles.width = AVATAR_W
        av.styles.height = ROW_H
        yield av
        with Vertical(classes="rowtext"):
            yield Static(self._name, classes="name")
            yield Static(self._preview, classes="preview")


class Proof(App):
    CSS = """
    Screen { background: #1e1e1e; }
    #list { width: 48; border-right: solid #242f38; }
    Row { height: 3; }
    Row.shade { background: #2b3240; }
    .rowtext { width: 1fr; padding: 0 0 0 1; }
    .rowtext > Static { height: 1; }
    /* Fixed colours, same on both row backgrounds (no auto-contrast, no dim). */
    .rowtext > .name { color: %(name)s; text-style: bold; }
    .rowtext > .preview { color: %(preview)s; }
    """ % {"name": NAME_COLOR, "preview": PREVIEW_COLOR}

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="list"):
            for r in ROWS:
                yield Row(*r)

    # Toggle to compare: True disables hover-motion reporting (the foot fix).
    DISABLE_HOVER_MOTION = True

    def on_mount(self) -> None:
        self.query_one("#list").focus()
        if self.DISABLE_HOVER_MOTION:
            self.call_after_refresh(self._quiet_hover)

    def _quiet_hover(self) -> None:
        # Textual enables \x1b[?1003h (report ALL mouse motion) to drive :hover.
        # Each hover refreshes the row under the pointer, and textual-image re-emits
        # that row's sixel every render -- which foot mis-clears, duplicating the
        # adjacent text until the next full repaint. Turning OFF any-event motion
        # (1003) stops bare hover from refreshing anything; button+wheel reporting
        # (1000) stays on, so wheel-scroll and clicks still work. Ghostty (Kitty
        # graphics) never had the problem. Written directly, once, after the first
        # frame so it lands between Textual's own writes.
        try:
            os.write(1, b"\x1b[?1003l")
        except OSError:
            pass

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("j", "scroll_down", "Down"),
        ("k", "scroll_up", "Up"),
        ("f", "page_down", "Page dn"),
        ("b", "page_up", "Page up"),
        ("g", "scroll_home", "Top"),
        ("G", "scroll_end", "Bottom"),
    ]

    def action_scroll_down(self) -> None:
        self.query_one("#list").scroll_relative(y=3, animate=False)

    def action_scroll_up(self) -> None:
        self.query_one("#list").scroll_relative(y=-3, animate=False)

    def action_page_down(self) -> None:
        self.query_one("#list").scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        self.query_one("#list").scroll_page_up(animate=False)

    def action_scroll_home(self) -> None:
        self.query_one("#list").scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        self.query_one("#list").scroll_end(animate=False)


if __name__ == "__main__":
    Proof().run()
