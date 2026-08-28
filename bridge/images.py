"""Render MMS images into the terminal.

TWO RENDERERS, sixel preferred.

Half blocks (U+2580: foreground paints the top pixel, background the bottom) cap
at cols x 2*rows pixels. In a reading pane that is about 48 x 28 -- 1,344 pixels,
smaller than a favicon, and photographs came out as unidentifiable smudges. That
is a hard ceiling, not a tuning problem: no arrangement of cells beats two pixels
each.

Sixel draws actual pixels and foot supports it, so it is used when available. The
reservation that kept it out originally still stands -- graphics protocols paint
outside the widget tree while Textual owns and repaints the screen -- so it is
switchable, and half blocks remain the fallback for a terminal without sixel or
when SMS_TUI_RENDER=blocks is set.

Everything else here works in attachment digests rather than pixels, so swapping
renderers changes nothing else.
"""

import os
from pathlib import Path

from rich.text import Text

try:
    from PIL import Image
    AVAILABLE = True
except ImportError:                     # renders a placeholder instead of crashing
    AVAILABLE = False

# Sixel is optional: the server does not need it, and a terminal without it falls
# back cleanly. SMS_TUI_RENDER=blocks forces the fallback.
# OFF BY DEFAULT. Sixel works -- the renderable emits real DCS data -- but Textual
# composites its own screen buffer, so the escape sequence does not survive to the
# terminal: foot draws an empty block where the image should be and ghostty draws
# nothing. textual-image ships a WIDGET that cooperates with the compositor, but a
# widget cannot live inside RichLog, which holds Rich renderables only. Making this
# work means rebuilding the reading pane out of widgets.
# SMS_TUI_RENDER=sixel opts in for anyone who wants to experiment.
SIXEL = None
if os.environ.get("SMS_TUI_RENDER", "blocks") == "sixel":
    try:
        from textual_image.renderable.sixel import Image as SIXEL
    except Exception:
        SIXEL = None

UPPER_HALF = "▀"


def render(path: Path, max_w: int = 40, max_h: int = 20):
    """An image for the reading pane: sixel if available, half blocks otherwise."""
    if SIXEL is not None:
        try:
            return SIXEL(str(path), width=max_w, height=max_h)
        except Exception:
            pass            # any sixel trouble falls through to blocks
    return render_blocks(path, max_w, max_h)


def render_blocks(path: Path, max_w: int = 40, max_h: int = 20) -> Text:
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


# Terminal cells are about twice as tall as they are wide. The exact figure is
# font-dependent and only knowable by querying a real terminal, so it is assumed:
# being a little out makes a picture slightly tall or wide, while asking a terminal
# that cannot answer returns zero and shows nothing at all.
CELL_ASPECT = 2.0


def fit(path: Path, max_cols: int, max_rows: int = 20) -> tuple[int, int]:
    """(cols, rows) that keep an image's proportions inside the given box.

    Clamping the rows alone was not enough: a tall photo hit the cap and was
    squashed back to square. When the height binds, the WIDTH has to come down.
    """
    if not AVAILABLE:
        return max_cols, min(max_rows, max(2, max_cols // 2))
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Exception:
        return max_cols, min(max_rows, max(2, max_cols // 2))
    if not w or not h:
        return max_cols, 2

    cols = max_cols
    rows = round((h / w) * cols / CELL_ASPECT)
    if rows > max_rows:
        rows = max_rows
        cols = max(4, round(rows * CELL_ASPECT * (w / h)))
    return cols, max(2, rows)


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
