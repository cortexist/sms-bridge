"""Render MMS images into the terminal.

Half-block rendering rather than Sixel or the Kitty protocol, deliberately.

foot does support Sixel, and textual-image can drive it -- but graphics protocols
put pixels on the terminal OUTSIDE the widget tree, while Textual owns the screen
and repaints it. Anything scrolled, resized or refreshed then leaves the image
stranded where the text used to be. Half blocks are ordinary cells: they scroll,
clip and repaint like everything else, at the cost of resolution.

The trick is that U+2580 UPPER HALF BLOCK gives two pixels per cell -- foreground
paints the top, background the bottom -- so a cell grid W x H shows W x 2H pixels.

Sixel remains the upgrade path if fidelity ever matters more than behaving like a
widget; it is a rendering swap, not a redesign, because everything else here works
in attachment digests rather than pixels.
"""

from pathlib import Path

from rich.text import Text

try:
    from PIL import Image
    AVAILABLE = True
except ImportError:                     # renders a placeholder instead of crashing
    AVAILABLE = False

UPPER_HALF = "▀"


def render(path: Path, max_w: int = 40, max_h: int = 20) -> Text:
    """An image as Text, at most max_w columns and max_h rows.

    max_h counts CELLS; each holds two pixel rows, so the sampled image is
    max_w x 2*max_h pixels.
    """
    if not AVAILABLE:
        return Text("[image: install python-pillow to display]", style="dim")
    try:
        img = Image.open(path)
        img = img.convert("RGB")
    except Exception as e:
        return Text(f"[unreadable image: {e.__class__.__name__}]", style="dim")

    # Terminal cells are about twice as tall as they are wide, and one cell already
    # carries two pixel rows -- so sampling height at 2x columns keeps the aspect
    # ratio right without any further correction.
    w, h = img.size
    scale = min(max_w / w, (max_h * 2) / h, 1.0)
    tw = max(1, int(w * scale))
    th = max(2, int(h * scale))
    if th % 2:
        th += 1                          # even number of pixel rows: one per half-cell
    img = img.resize((tw, th), Image.LANCZOS)
    px = img.load()

    out = Text(no_wrap=True, overflow="crop")
    for y in range(0, th, 2):
        for x in range(tw):
            top = px[x, y]
            bottom = px[x, y + 1] if y + 1 < th else top
            out.append(UPPER_HALF, style=f"rgb({top[0]},{top[1]},{top[2]}) "
                                         f"on rgb({bottom[0]},{bottom[1]},{bottom[2]})")
        if y + 2 < th:
            out.append("\n")
    return out


def describe(part: dict) -> str:
    """One line for a part that is not being drawn."""
    mime = part.get("mime", "?")
    size = part.get("size") or 0
    name = part.get("name") or ""
    why = part.get("skipped")
    bits = [mime]
    if name:
        bits.append(name)
    if size:
        bits.append(f"{size/1024:.0f} KB" if size < 1024 * 1024
                    else f"{size/1048576:.1f} MB")
    if why:
        bits.append(f"not transferred: {why}")
    return "[" + ", ".join(bits) + "]"
