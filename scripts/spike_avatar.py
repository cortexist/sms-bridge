#!/usr/bin/env python3
"""Half-block avatar spike — eyeball icon fidelity at avatar size before we
commit to sixel.

Renders the dark-theme icon set as half-block "pixels" (▀ with the top pixel as
foreground and the bottom pixel as background, so one text cell = two vertical
pixels), composited over the two real conversation-list backgrounds. Prints to
stdout; run it inside foot (or any truecolor terminal) and look.

    python3 scripts/spike_avatar.py

Why half-block and not sixel: it composites as ordinary coloured text cells, so
it drops straight into an OptionList row with no framework fight and no terminal
graphics protocol — the whole reason to check whether it's good enough first.

The two backgrounds are the ones the list actually uses (resolved from the
Textual dark theme): SURFACE for normal rows, SHADE for the zebra-striped rows.
Selection no longer swaps the background, so these two are the only variants an
icon needs.
"""

import sys
from pathlib import Path

from PIL import Image
from rich.console import Console
from rich.text import Text

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "dark"

# From bridge.tui: normal-row surface is #1E1E1E; the zebra band is ROW_SHADE
# "#2b3240" laid over it. At full opacity the shaded rows read as ~#2b3240.
SURFACE = (0x1E, 0x1E, 0x1E)
SHADE = (0x2B, 0x32, 0x40)

# Candidate avatar footprints, in TEXT CELLS. A cell is ~1x2 px of half-block, and
# a terminal cell is roughly twice as tall as wide, so cells_w x cells_h maps to
# (cells_w) x (cells_h*2) pixels at roughly 1:1 aspect.
SIZES = [(3, 3), (4, 4), (6, 6)]


def composite(img: Image.Image, bg: tuple[int, int, int]) -> Image.Image:
    """Flatten an RGBA icon onto a solid background — the blend the transparent
    PNGs need before they can be shown as opaque pixels."""
    base = Image.new("RGBA", img.size, bg + (255,))
    return Image.alpha_composite(base, img).convert("RGB")


def halfblock(img: Image.Image, cells_w: int, cells_h: int) -> Text:
    """Downscale to (cells_w) x (cells_h*2) pixels and pack each vertical pair of
    pixels into one ▀ cell (fg=top, bg=bottom)."""
    px_w, px_h = cells_w, cells_h * 2
    small = img.resize((px_w, px_h), Image.LANCZOS)
    out = Text(no_wrap=True, overflow="crop")
    for row in range(0, px_h, 2):
        for col in range(px_w):
            top = small.getpixel((col, row))
            bot = small.getpixel((col, row + 1))
            out.append(
                "▀",
                style=f"rgb({top[0]},{top[1]},{top[2]}) on rgb({bot[0]},{bot[1]},{bot[2]})",
            )
        out.append("\n")
    return out


def main() -> int:
    icons = sorted({p.name for p in ASSETS.glob("*-45-dt.png")})
    if not icons:
        print(f"no *-45-dt.png icons under {ASSETS}", file=sys.stderr)
        return 1

    con = Console()
    for bg_name, bg in (("SURFACE #1E1E1E (normal rows)", SURFACE),
                        ("SHADE #2B3240 (zebra rows)", SHADE)):
        con.rule(f"[bold]{bg_name}[/]")
        for name in icons:
            src = Image.open(ASSETS / name).convert("RGBA")
            flat = composite(src, bg)
            label = name.replace("-45-dt.png", "")
            sizes = "  ".join(f"{w}x{h}" for (w, h) in SIZES)
            con.print(f"[dim]{label}[/]  ([dim]{sizes} cells[/])")
            for (w, h) in SIZES:
                con.print(halfblock(flat, w, h), end="")
            con.print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
