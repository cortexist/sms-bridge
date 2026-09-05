"""sms-bridge TUI: read view over the message archive.

Layout follows phone/tablet messaging apps (iMessage, QUIK) rather than a log
table. A conversation list entry is THREE lines, not one -- sender and time on the
first, up to two wrapped lines of the actual message under it. One narrow row
cannot carry sender + time + count + preview without truncating all four, and the
preview is the part you actually read when triaging.

The reading pane is a message chain: bubbles, incoming left, outgoing indented
right, with date rules between days.

ACTIONS ARE QUEUED, NOT APPLIED HERE. Pressing `d` does not delete anything on
this machine; it queues a command the phone collects and executes, because the
phone owns the message store. So a delete is `pending` until the phone acks, and
the footer says so rather than pretending it already landed. Applied near-instantly
when messages are flowing (the phone piggybacks on its next forward), otherwise
within the 15-minute poll.

Delete acts on the whole conversation, matching what a messaging app does -- and
deliberately NOT on individual messages, which would mean deleting from the middle
of a chain. Trimming by age exists for that (`delete_old_messages`) and is
evaluated on the phone, since this archive has no backfill and cannot tell where a
chain really begins.

The archive holds only what has been forwarded since the bridge started -- there
is no backfill of the phone's history yet, and the footer says so, because a
thread that looks like it began yesterday probably did not.

Run:  sms-tui        (or: python -m bridge.tui from ~/Workspaces/sms-bridge)
"""

import io
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.measure import Measurement
from rich.padding import Padding
from rich.panel import Panel
from rich.cells import cell_len
from rich.console import Group
from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, OptionList, Static
from textual.widgets.option_list import Option

from bridge import images, junk, store

# The chain pane is built from WIDGETS, not RichLog renderables, so that images can
# be real image widgets. textual-image's widget cooperates with Textual's compositor
# the way ratatui-image cooperates with ratatui's render loop; its *renderable*, used
# inside RichLog, had its escape sequence eaten by the compositor and drew an empty
# box in foot and nothing in ghostty.
try:
    from textual_image.widget import Image as ImageWidget
except Exception:
    ImageWidget = None

# KITTY_GRAPHICS: the terminal places images anchored to cells (Kitty/TGP, e.g.
# ghostty) rather than sixel (foot). Only relevant to the CONTENT pane's message
# images now: cell-anchored images scroll and clear natively, so the repaint hack
# ChainPane uses is needed on sixel (foot) and skipped on Kitty (ghostty). The
# conversation-list avatars are font GLYPHS, not images, so they need none of this.
# Detected at import, before Textual starts (the terminal can't be queried after).
KITTY_GRAPHICS = False
if ImageWidget is not None:
    try:
        from textual_image.renderable import Image as _AutoRenderable
        from textual_image.renderable.sixel import Image as _SixelRenderable
        KITTY_GRAPHICS = _AutoRenderable is not _SixelRenderable
    except Exception:
        KITTY_GRAPHICS = False

# Off-screen console purely for measuring how wide a bubble will actually render.
# RichLog sizes a write to the renderable's OWN measured width, so Align.right never
# had room to align into -- 19 columns of the pane sat unused. Measuring lets the
# left pad be computed exactly instead.
_MEASURE = Console(width=400, file=io.StringIO(), force_terminal=False)

REFRESH_S = 3.0
SHOW_LAST = 80           # newest messages rendered; widgets cost more than text
# Small on purpose: each fetch is a megabyte or two off the phone, and a run that
# tries to do dozens at once outlives its worker and loses the lot.
FETCH_LIMIT = 8          # images requested per keypress
# Paired with THUMB_PX=320 on the phone, which fills 32-40 cells at foot's cell
# width. Shrinking this box was the other way to fix the blur; raising the source
# resolution costs 27 MB across the archive and keeps the picture large.
# 32, not 40: a 320px thumbnail fills roughly 32-40 cells depending on the font, so
# the lower end is the safe match -- sharper, and a smaller sixel leaves less residue
# behind when the pane scrolls.
IMG_W = 32               # cells wide; height follows the image's aspect ratio
IMG_H = 16               # fallback block renderer only
IMG_MAX_H = 16           # tallest an image may get before its width is reduced
OPEN_LIMIT = 20          # images considered when opening externally
BUBBLE_FRAC = 0.82       # widest a bubble may get, as a fraction of the reading pane
BUBBLE_MIN = 20          # narrow enough that a ~65 column pane still works
PREVIEW_LINES = 2
PREVIEW_WIDTH = 36


def pretty_addr(a: str) -> str:
    """+18005550199 -> +1 (800) 555-0199. Long digit strings are unreadable."""
    if not a:
        return "?"
    d = re.sub(r"\D", "", a)
    if len(d) == 11 and d.startswith("1"):
        return f"+1 ({d[1:4]}) {d[4:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return a                      # shortcodes and alphanumeric senders, left alone


def when(ts: int) -> str:
    if not ts:
        return ""
    t = datetime.fromtimestamp(ts)
    now = datetime.now()
    if t.date() == now.date():
        return t.strftime("%H:%M")
    if (now.date() - t.date()).days == 1:
        return "Yesterday"
    if (now.date() - t.date()).days < 7:
        return t.strftime("%A")
    if t.year != now.year:
        # "Dec 21" is ambiguous once the archive goes back to 2016.
        return t.strftime("%b %d %Y")
    return t.strftime("%b %d")


def wrap(text: str, width: int, lines: int) -> list[str]:
    """Preview wrapped to at most `lines`, ellipsised. Newlines become spaces."""
    words, out, cur = (text or "").replace("\n", " ").split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= width:
            cur = f"{cur} {w}".strip()
        else:
            out.append(cur)
            cur = w
            if len(out) == lines:
                break
    if cur and len(out) < lines:
        out.append(cur)
    if not out:
        return [""]
    consumed = sum(len(x) for x in out) + len(out)
    if consumed < len(text or "") - 1:
        out[-1] = out[-1][: width - 1].rstrip() + "…"
    return out


AVATAR_W = 6
# Faint band behind alternate entries. Tuned against the default dark theme; it has
# to be visible enough to group three rows and quiet enough not to read as selection.
ROW_SHADE = "on #2b3240"
AVATAR_INDENT = 2        # columns before the avatar glyph
OVERFILL = 12            # pad past the visible edge so the shade reaches it

# Selection is shown as a left accent bar in column 0, not a background swap.
ACCENT_GLYPH = "▌"  # ▌ full-height left half block
ACCENT_STYLE = "bold cyan"

# Nerd Font glyphs (the Font Awesome range, present in every Nerd Font build),
# rendered as ORDINARY TEXT -- so they scroll cleanly and take the cell's colour in
# any terminal, no sixel or Kitty graphics. The terminal's font fallback must supply
# a Nerd Font (as configured for waybar); until then the codepoint shows as a box.
# Single-cell on purpose; a larger icon would need a purpose-built multi-cell font.
# Add per-kind codepoints here as the icon set grows (image/audio/video/pay/...).
ICON_KEY = "\uf084"       # nf-fa-key: newest inbound message is a 2FA code
ICON_MESSAGE = "\U000F0188"   # nf-fa-comment: everything else
ICON_PIN = "\uf435"       # pinned-to-top marker on the name line
ICON_STYLE = "#e2e2e2"    # normal text tone, matching the name text


def avatar(thread: dict) -> Text:
    """The single-cell icon glyph for a conversation.

    A key when the NEWEST inbound message is a 2FA code -- a per-message fact, not a
    property of the sender: a dedicated 2FA server keeps the key permanently, while a
    contact that once sent a code shows it only until their next ordinary text. A
    message bubble otherwise.

    Returns the BARE glyph; padding to the avatar column is the caller's job, sized
    with cell width (a glyph may be one character but two columns wide).
    """
    cp = ICON_KEY if thread.get("last_code") else ICON_MESSAGE
    return Text(cp, style=ICON_STYLE)


def date_rule(label: str, width: int) -> Text:
    """A full-width centred date separator.

    Built by hand rather than with rich.Rule: RichLog sizes each write to the
    renderable's own measured width, and a Rule measures tiny, so it collapsed to
    "- Frid... -" instead of spanning the pane. Text of an exact width cannot be
    re-measured into something smaller.
    """
    tag = f" {label} "
    dashes = max(0, width - len(tag) - 1)      # -1 keeps the line off the scrollbar
    left = dashes // 2
    out = Text(no_wrap=True, overflow="crop", style="dim")
    out.append("\u2500" * left)
    out.append(tag)
    out.append("\u2500" * (dashes - left))
    out.append(" ")
    return out


def _lead(n: int, accent: bool) -> Text:
    """The n-column left margin of a row line, carrying the selection accent bar
    in column 0 when this row is selected. Width is always exactly n, so the
    accent never shifts the columns to its right."""
    s = Text(no_wrap=True, overflow="crop")
    if accent:
        s.append(ACCENT_GLYPH, style=ACCENT_STYLE)
        s.append(" " * (n - 1))
    else:
        s.append(" " * n)
    return s


def thread_row(t: dict, width: int, index: int = 0, accent: bool = False) -> Group:
    """One conversation: avatar column, then sender/time and preview.

    Plain Text lines rather than a Table. A Table.grid reflows its columns whenever
    their total exceeds the width it is rendered at -- and inside an OptionList that
    width is smaller than the widget's content_size -- so the six-wide avatar column
    was being shrunk and ellipsised into "...". Text with overflow="crop" cannot
    reflow: it either fits or is cut, which is the behaviour wanted here.

    The avatar glyph is a font ICON (see avatar()), rendered as ordinary text so it
    scrolls and colours natively in any terminal -- no sixel, no Kitty graphics.
    """
    body_w = max(12, width - AVATAR_W)

    stamp = when(t["last"])
    name = (t.get("name") or pretty_addr(t["addr"]))
    # Single-cell glyph on purpose: the padding below counts with len(), and a
    # double-width emoji would shift the timestamp column by one.
    pin = ICON_PIN + " " if t.get("pinned") else ""
    name_max = max(4, body_w - len(stamp) - 1 - len(pin))
    if len(name) > name_max:
        name = name[: name_max - 1] + "…"

    head = _lead(AVATAR_W, accent)
    if pin:
        head.append(pin, style="bold yellow")
    head.append(name, style="bold")
    head.append(" " * max(1, body_w - len(pin) - len(name) - len(stamp)))
    head.append(stamp, style="dim")

    preview = wrap(t["preview"], body_w, PREVIEW_LINES)
    preview += [""] * (PREVIEW_LINES - len(preview))

    # The avatar sits on the SECOND row, indented two columns: against a three-row
    # entry that reads as vertically centred, where top-left read as a bullet.
    av_row = _lead(AVATAR_INDENT, accent)
    av_row.append_text(avatar(t))
    # cell_len already counts the indent and the glyph, so pad to AVATAR_W outright --
    # subtracting the indent a second time left this row one column left of the others.
    av_row.append(" " * max(1, AVATAR_W - cell_len(av_row.plain)))
    av_row.append(preview[0], style="dim")

    tail = _lead(AVATAR_W, accent)
    tail.append(preview[1], style="dim")

    lines = [head, av_row, tail]

    # Pad BEFORE styling, and past the visible edge: styling first left the padding
    # unstyled, so the band only sat behind the text instead of filling the row.
    # Overflow is cropped, so overshooting is free.
    for ln in lines:
        ln.pad_right(max(0, width + OVERFILL - cell_len(ln.plain)))
        if index % 2:
            ln.stylize(ROW_SHADE)
    return Group(*lines)


class ThreadList(OptionList):
    """OptionList with the wrap-around cursor removed.

    Stock OptionList treats the list as a ring: cursor-down on the last row jumps
    to the first. Harmless when the order is pure recency; with pinned threads
    held at the top it turns "scrolled past the end" into "teleported back to the
    pins", which reads as the list reshuffling itself. Clamp at the edges instead.
    """

    def action_cursor_down(self) -> None:
        if self.highlighted is None or self.highlighted < self.option_count - 1:
            super().action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.highlighted is None or self.highlighted > 0:
            super().action_cursor_up()


class ChainPane(VerticalScroll):
    """The message chain, with a full repaint on scroll -- on sixel terminals only.

    Sixel data stays where the terminal painted it when Textual scrolls its cells,
    leaving residue (most visibly on thin dim rules, which nothing else repaints
    over). Forcing a whole-screen repaint after a scroll scrubs it. Kitty graphics
    (ghostty) anchors images to cells and clears them on scroll natively, so the
    repaint is not only unnecessary there, it adds flashing -- hence gated on
    KITTY_GRAPHICS being false.
    """

    def watch_scroll_y(self, old: float, new: float) -> None:
        super().watch_scroll_y(old, new)
        if not KITTY_GRAPHICS and int(old) != int(new):
            self.screen.refresh(repaint=True)


class ConfirmDelete(ModalScreen[bool]):
    """QUIK asks before deleting; so does this. Same action, same friction.

    The wording names what actually happens -- the whole conversation, on the phone,
    permanently -- because a delete here goes through to the system provider and
    there is no undo on either side.
    """

    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    CSS = """
    ConfirmDelete { align: center middle; }
    #box {
        grid-size: 2 3; grid-gutter: 1 2; padding: 1 2;
        width: 62; height: auto; border: thick $error; background: $surface;
    }
    #question { column-span: 2; height: auto; }
    #warn { column-span: 2; height: auto; color: $text-muted; }
    """

    def __init__(self, addr: str, count: int) -> None:
        super().__init__()
        self.addr, self.count = addr, count

    def compose(self) -> ComposeResult:
        with Grid(id="box"):
            yield Static(
                f"Delete the conversation with [b]{pretty_addr(self.addr)}[/b]?\n"
                f"{self.count} message(s) in this archive.", id="question")
            yield Static(
                "Deletes on the phone as well, from the system message store. "
                "Not recoverable.", id="warn")
            yield Button("Delete", variant="error", id="yes")
            yield Button("Cancel", variant="primary", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class BridgeTUI(App):
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #threads {
        width: 47;
        border: none;
        border-right: solid $panel-darken-2;
        background: $surface;
    }
    /* No focus box around the list: selection is the left accent bar instead. */
    #threads:focus { border: none; border-right: solid $panel-darken-2; }
    #threads > .option-list--option { padding: 0; }
    /* Suppress the native highlight background in both blurred and focused states,
       so the only selection cue is the accent bar drawn into the row. */
    #threads > .option-list--option-highlighted,
    #threads:focus > .option-list--option-highlighted {
        background: transparent;
        text-style: none;
    }
    #convo { width: 1fr; height: 1fr; padding: 0 1; }
    #convo > Static { height: auto; }
    /* height:auto lets the widget keep the image's aspect ratio; max-height stops a
       tall photo from taking the whole pane. */
    #convo > .bridge-img { height: auto; max-height: 20; margin: 0 0 1 0; }
    #status { height: auto; padding: 0 1; color: $text-muted; background: $panel; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "reload", "Reload"),
        Binding("g", "top", "Newest"),
        Binding("c", "codes_only", "2FA only"),
        Binding("p", "pin", "Pin/unpin"),
        Binding("d", "delete", "Delete chain"),
        Binding("j", "junk", "Junk"),
        Binding("u", "not_junk", "Not junk"),
        Binding("Q", "show_junk", "Quarantine"),
        Binding("f", "fetch", "Fetch images"),
        Binding("o", "open_images", "Open"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.threads: list = []
        self.selected: str | None = None
        self.selected_thread: dict | None = None
        self.codes_only = False
        self.show_junk = False
        self._accent_index: int | None = None   # row currently carrying the accent bar

    def compose(self) -> ComposeResult:
        # No Header: it only ever offered command search, which reads as message
        # search and isn't. Conversation/message search will be its own feature.
        with Horizontal(id="body"):
            yield ThreadList(id="threads")
            yield ChainPane(id="convo")
        # Outside #body so it spans the window rather than being clipped to the
        # reading pane's width.
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.reload()
        # Sizes are not assigned until after the first refresh; the initial render
        # above therefore uses placeholder widths. Re-render once layout is real.
        self.call_after_refresh(self.action_reload)
        self.set_interval(REFRESH_S, self.reload)

    # ------------------------------------------------------------------ data

    def reload(self) -> None:
        store.touch_presence()   # "a human is at the desk": the phone's live link keys on this
        rows = store.threads()
        # Quarantined conversations leave the main list and appear only under Q.
        # Hidden, never deleted -- the whole point of quarantine over blocking is
        # that a wrong verdict costs a keystroke rather than a message.
        verdicts = store.verdicts()
        quarantined = {k for k, v in verdicts.items() if v["junk"]}
        rows = [t for t in rows
                if (store.verdict_key(t) in quarantined) == self.show_junk]
        if self.codes_only:
            rows = [t for t in rows if t["codes"]]
        self._verdicts = verdicts

        # Rebuild only on a real change: a poll every few seconds must not yank the
        # selection out from under someone who is reading.
        sig = [(t["addr"], t["count"], t["last"], t["pinned"]) for t in rows]
        if sig == [(t["addr"], t["count"], t["last"], t.get("pinned")) for t in self.threads]:
            self.refresh_status()
            return

        self.threads = rows
        lv = self.query_one("#threads", OptionList)
        keep = self.selected
        lv.clear_options()
        self._accent_index = None   # options rebuilt; old index no longer valid
        width = self._list_width(lv)
        lv.add_options([Option(thread_row(t, width, i), id=self._key(t))
                        for i, t in enumerate(rows)])

        if rows:
            idx = next((i for i, t in enumerate(rows) if self._key(t) == keep), 0)
            lv.highlighted = idx
            self.show_thread(rows[idx])
            self._set_accent(idx)
        else:
            self.query_one("#convo", ChainPane).remove_children()
            self.selected = None
        self.refresh_status()

    @staticmethod
    def _list_width(lv) -> int:
        """Columns actually available to a row.

        content_size excludes padding and border but NOT the scrollbar, which is
        always present here, so one more column comes off. Six was measured, not
        reasoned -- redo it with SMSTUI_SLACK if the chrome changes. Erring large is
        safe (rows crop); erring small ate the avatars and the timestamps.
        """
        w = lv.content_size.width or lv.size.width or 44
        return max(20, w - int(os.environ.get("SMSTUI_SLACK", "3")))

    def on_resize(self, event) -> None:
        self.threads = []          # rows are pre-rendered at a fixed width
        self.reload()

    @staticmethod
    def _key(t: dict) -> str:
        """Stable identity for a conversation across refreshes."""
        return f"t{t['thread']}" if t.get("thread") is not None else f"a{t['norm']}"

    def refresh_status(self) -> None:
        pend = store.pending()
        nq = sum(1 for v in getattr(self, "_verdicts", {}).values() if v["junk"])
        bits = [f"{len(store.messages())} messages",
                f"{len(self.threads)} " + ("quarantined" if self.show_junk else "threads")]
        if not self.show_junk and nq:
            bits.append(f"{nq} in quarantine (Q)")
        if pend:
            # Surfaced, not hidden: nothing drains this queue until the phone-side
            # worker exists, so these will sit here.
            bits.append(f"{len(pend)} command(s) pending - no phone worker yet")
        if self.codes_only:
            bits.append("filter: 2FA only")
        # This used to read "forwarded only - no backfill", which was true for about
        # a day and has been a lie ever since the history import. State the span the
        # archive actually covers instead of asserting what it lacks.
        msgs = store.messages()
        oldest = min((m.get("ts") or m.get("rx", 0) for m in msgs), default=0)
        if oldest:
            bits.append("since " + datetime.fromtimestamp(oldest).strftime("%b %Y"))
        self.query_one("#status", Static).update(" · ".join(bits))

    def show_thread(self, t: dict) -> None:
        self.selected = self._key(t)
        self.selected_thread = t
        pane = self.query_one("#convo", ChainPane)
        pane.remove_children()
        # Hide the pane until it has been scrolled to the newest message. Otherwise
        # Textual paints from the TOP for a frame -- rendering (and, on the image
        # widgets, encoding) the oldest messages, so years-old pictures flash by
        # before scroll_end jumps to the bottom. visibility:hidden keeps the layout
        # (so scroll_end can measure the height) but paints nothing.
        pane.styles.visibility = "hidden"

        tid = t.get("thread")
        if tid is not None:
            msgs = [m for m in store.messages() if m.get("thread") == tid]
        else:
            aliases = set(t.get("addrs") or [t["addr"]])
            msgs = [m for m in store.messages()
                    if m.get("thread") is None and m.get("addr") in aliases]
        # ts (phone message time), not rx (box sync time): a delivery delayed behind
        # an outage arrives hours after it was sent, and showing the sync time both
        # mislabels the bubble and misorders it against messages that synced promptly.
        msgs = sorted(msgs, key=lambda m: m.get("ts") or m.get("rx", 0))

        widgets = []
        # Fewer than the RichLog version carried: every message is now one or more
        # widgets, and several hundred of those cost about a second to mount.
        if len(msgs) > SHOW_LAST:
            widgets.append(Static(Text(
                f"... {len(msgs) - SHOW_LAST} earlier message(s) not shown",
                style="dim")))
            msgs = msgs[-SHOW_LAST:]

        pane_w = max(BUBBLE_MIN + 4, (pane.content_size.width or 60) - 1)
        bubble_w = max(BUBBLE_MIN, int(pane_w * BUBBLE_FRAC))

        widgets.append(Static(Text((t.get("name") or pretty_addr(t["addr"])), style="bold")))

        day = None
        for m in msgs:
            when_ = datetime.fromtimestamp(m.get("ts") or m.get("rx", 0))
            if when_.date() != day:
                day = when_.date()
                label = when_.strftime("%A, %B %-d")
                if when_.year != datetime.now().year:
                    label = when_.strftime("%A, %B %-d, %Y")
                widgets.append(Static(date_rule(label, pane_w)))

            outgoing = m.get("dir") == "out"
            body = m.get("body") or "(no body stored)"
            tag = when_.strftime("%H:%M")
            if m.get("code"):
                tag += f"  •  code {m['code']}"
            if m.get("kind") != "sms":
                tag += f"  •  {m.get('kind')}"

            bubble = Panel(
                Text(body),
                title=tag,
                title_align="right" if outgoing else "left",
                border_style="cyan" if outgoing else "green",
                expand=False,
                width=None if len(body) < bubble_w else bubble_w,
                padding=(0, 1),
            )
            if outgoing:
                w = min(pane_w, Measurement.get(
                    _MEASURE, _MEASURE.options.update_width(pane_w), bubble).maximum)
                widgets.append(Static(Padding(bubble, (0, 0, 0, max(0, pane_w - w)))))
            else:
                widgets.append(Static(bubble))

            for part in (m.get("parts") or []):
                shown = None
                for key in ("thumb", "sha"):
                    d = part.get(key)
                    if d and store.has_attachment(d):
                        shown = d
                        break
                if shown and str(part.get("mime", "")).startswith("image/"):
                    if ImageWidget is not None:
                        # Real pixels where the terminal supports them: the widget
                        # negotiates sixel/kitty itself and falls back to half cells.
                        path = store.attachment_path(shown)
                        iw = ImageWidget(str(path))
                        iw.add_class("bridge-img")
                        # Height computed from the image's own proportions rather than
                        # pinned (which squashed every picture into the same square) or
                        # left auto (which asks the terminal for its cell size -- fine
                        # in a real terminal, zero anywhere without a tty).
                        cols, rows = images.fit(path, min(bubble_w, IMG_W), IMG_MAX_H)
                        iw.styles.width = cols
                        iw.styles.height = rows
                        widgets.append(iw)
                    else:
                        widgets.append(Static(images.render_blocks(
                            store.attachment_path(shown),
                            max_w=min(bubble_w, IMG_W), max_h=IMG_H)))
                elif part.get("sha") and str(part.get("mime", "")).startswith("image/"):
                    widgets.append(Static(Text(
                        "  " + images.describe(part) + "  — f to fetch", style="dim")))
                else:
                    widgets.append(Static(Text("  " + images.describe(part), style="dim")))

        pane.mount_all(widgets)

        # Reveal only after layout settles and we've jumped to the end, so the first
        # visible frame is already at the newest message -- no top-of-thread flash.
        def _reveal() -> None:
            pane.scroll_end(animate=False)
            pane.styles.visibility = "visible"

        self.call_after_refresh(_reveal)

    # --------------------------------------------------------------- actions

    def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted) -> None:
        if 0 <= event.option_index < len(self.threads):
            self.show_thread(self.threads[event.option_index])
            self._set_accent(event.option_index)

    def _set_accent(self, new_index: int) -> None:
        """Move the selection accent bar to `new_index`, re-rendering only the row
        that loses it and the one that gains it. Cheap: two prompt replacements,
        no full list rebuild, so cursor movement stays instant."""
        if new_index == self._accent_index:
            return
        lv = self.query_one("#threads", OptionList)
        width = self._list_width(lv)
        prev = self._accent_index
        self._accent_index = new_index
        for i, accent in ((prev, False), (new_index, True)):
            if i is None or not (0 <= i < len(self.threads)):
                continue
            lv.replace_option_prompt_at_index(
                i, thread_row(self.threads[i], width, i, accent=accent))

    def action_reload(self) -> None:
        self.threads = []
        self.reload()

    def action_top(self) -> None:
        if self.threads:
            self.query_one("#threads", OptionList).highlighted = 0

    def action_pin(self) -> None:
        """Toggle pin on the selected conversation, mirrored to the phone.

        The desktop record takes effect here immediately; the mark_pinned /
        mark_unpinned command reaches the phone on its next drain, and the phone's
        pushed state then owns the entry (see store.set_pin). Address-only threads
        from before backfill have no thread id to command, so those stay local.
        """
        t = self.selected_thread
        if not t:
            return
        cur = store.pins().get(store.verdict_key(t))
        now = not (cur and cur["pinned"])
        store.set_pin(t.get("thread"), t["addr"], now, "desktop")
        if t.get("thread") is not None:
            store.enqueue("mark_pinned" if now else "mark_unpinned",
                          threads=[t["thread"]])
        self.reload()

    def action_delete(self) -> None:
        """Queue a whole-conversation delete, after confirming."""
        t = getattr(self, "selected_thread", None)
        if not t:
            return

        def then(confirmed: bool | None) -> None:
            if not confirmed:
                return
            # Every spelling, so a delete cannot leave half a conversation behind.
            args = {"addrs": sorted(set(t.get("addrs") or [t["addr"]]))}
            # Prefer the phone's own thread id when we have it; address matching is
            # the fallback for records forwarded before that field existed.
            if t.get("thread") is not None:
                args["threads"] = [t["thread"]]
            store.enqueue("delete_conversations", **args)
            # Deliberately NOT removed from the archive here. It goes when the phone
            # acks, so a queued-but-unapplied delete cannot make the two sides
            # disagree -- the row stays and the footer shows the command pending.
            self.notify(f"Delete queued for {pretty_addr(t['addr'])} - "
                        f"applies when the phone next polls", timeout=6)
            self.refresh_status()

        self.push_screen(ConfirmDelete(t["addr"], t["count"]), then)

    def _verdict(self, junk_flag: bool) -> None:
        t = getattr(self, "selected_thread", None)
        if not t:
            return
        store.set_verdict(t.get("thread"), t["addr"], junk_flag, "desktop")
        # Mirror it to the phone: QUIK's block is app-local and reversible, so the
        # same conversation is hidden in both places without deleting anything.
        try:
            op = "mark_blocked" if junk_flag else "mark_unblocked"
            if t.get("thread") is not None:
                store.enqueue(op, threads=[t["thread"]])
        except Exception:
            pass
        self.notify(("Quarantined " if junk_flag else "Released ")
                    + (t.get("name") or pretty_addr(t["addr"])), timeout=4)
        self.threads = []
        self.reload()

    def action_fetch(self) -> None:
        """Request the images in this conversation that we do not hold.

        Backfill deliberately records digests without bytes, so a historic picture
        lives on the phone until it is asked for. Bounded per press: opening a
        thousand-image thread must not queue a thousand transfers.
        """
        t = getattr(self, "selected_thread", None)
        if not t:
            return
        tid = t.get("thread")
        want = []
        for m in store.messages():
            if tid is not None and m.get("thread") != tid:
                continue
            for p in (m.get("parts") or []):
                sha = p.get("sha")
                if (sha and str(p.get("mime", "")).startswith("image/")
                        and not store.has_attachment(sha)):
                    want.append((m.get("rx", 0), sha, m["id"]))
        # NEWEST FIRST. Taking them in archive order fetched the oldest images in the
        # conversation -- 2019 in a thread whose visible messages were 2020 -- so the
        # pictures being downloaded were never the ones on screen.
        want.sort(reverse=True)
        want = [(sha, mid) for _, sha, mid in want]
        if not want:
            self.notify("Nothing to fetch in this conversation", timeout=4)
            return
        for sha, mid in want[:FETCH_LIMIT]:
            store.enqueue("fetch_attachment", sha=sha, message=mid)
        self.notify(f"Requested {min(len(want), FETCH_LIMIT)} image(s)"
                    + (f" of {len(want)}" if len(want) > FETCH_LIMIT else "")
                    + " — arrives on the phone's next poll", timeout=6)
        self.refresh_status()

    def action_open_images(self) -> None:
        """Open this conversation's pictures in the desktop image viewer.

        A terminal is a poor place to look at a photograph: half blocks give two
        pixels a cell, so even the whole pane is a couple of thousand pixels. The
        inline render answers "is there a picture and roughly what"; this answers
        "what is it". Prefers the full original when held, falls back to the
        thumbnail, and says so rather than silently showing the small one.
        """
        t = getattr(self, "selected_thread", None)
        if not t:
            return
        tid = t.get("thread")
        paths, thumbs_only, missing = [], 0, 0
        for m in sorted((x for x in store.messages()
                         if tid is None or x.get("thread") == tid),
                        key=lambda x: -x.get("rx", 0)):
            for p in (m.get("parts") or []):
                if not str(p.get("mime", "")).startswith("image/"):
                    continue
                full, thumb = p.get("sha"), p.get("thumb")
                if full and store.has_attachment(full):
                    paths.append(store.attachment_path(full))
                elif thumb and store.has_attachment(thumb):
                    paths.append(store.attachment_path(thumb))
                    thumbs_only += 1
                else:
                    missing += 1
            if len(paths) >= OPEN_LIMIT:
                break

        if not paths:
            self.notify("No images held for this conversation — f to fetch", timeout=5)
            return
        try:
            subprocess.Popen(["xdg-open", str(paths[0])],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            self.notify(f"Could not open: {e.__class__.__name__}", timeout=5)
            return
        note = f"Opened {paths[0].name[:12]}…"
        if thumbs_only:
            note += f" ({thumbs_only} of these are thumbnails — f for originals)"
        self.notify(note, timeout=6)

    def action_junk(self) -> None:
        self._verdict(True)

    def action_not_junk(self) -> None:
        self._verdict(False)

    def action_show_junk(self) -> None:
        self.show_junk = not self.show_junk
        self.threads = []
        self.reload()

    def action_codes_only(self) -> None:
        self.codes_only = not self.codes_only
        self.threads = []
        self.reload()


def main() -> int:
    BridgeTUI().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
