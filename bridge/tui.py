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
from textual.containers import Grid, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from bridge import store

# Off-screen console purely for measuring how wide a bubble will actually render.
# RichLog sizes a write to the renderable's OWN measured width, so Align.right never
# had room to align into -- 19 columns of the pane sat unused. Measuring lets the
# left pad be computed exactly instead.
_MEASURE = Console(width=400, file=io.StringIO(), force_terminal=False)

REFRESH_S = 3.0
SHOW_LAST = 300          # newest messages rendered per conversation
BUBBLE_FRAC = 0.82       # widest a bubble may get, as a fraction of the reading pane
BUBBLE_MIN = 20          # narrow enough that a ~65 column pane still works
PREVIEW_LINES = 2
PREVIEW_WIDTH = 36


def pretty_addr(a: str) -> str:
    """+18449963776 -> +1 (844) 996-3776. Long digit strings are unreadable."""
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

# Stable per-contact colour, so a conversation keeps the same badge between sessions
# and you learn to recognise it by shape rather than by reading the number.
AVATAR_COLOURS = ("cyan", "green", "magenta", "yellow", "blue", "red",
                  "bright_cyan", "bright_green", "bright_magenta", "bright_blue")


def avatar(thread: dict) -> Text:
    """Exactly AVATAR_W cells on the left of every row.

    A key for anything that has sent a 2FA code -- those are the threads worth
    picking out at a glance. Everything else gets a coloured block for now; the
    space is reserved so a real avatar can go here later without moving anything.

    Padded by CELL width, not character count: the key emoji occupies two columns
    but is one character, so len() would size the column wrong.
    """
    if thread["codes"]:
        t = Text("\U0001f511", style="yellow")
    else:
        key = thread.get("norm") or thread["addr"]
        colour = AVATAR_COLOURS[sum(map(ord, key)) % len(AVATAR_COLOURS)]
        t = Text("\u2588\u2588", style=colour)
    # Returns the BARE glyph, two cells wide. Padding is the caller's job: it sits at
    # a different offset on the avatar row than the column width would imply.
    return t


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


def thread_row(t: dict, width: int, index: int = 0) -> Group:
    """One conversation: avatar column, then sender/time and preview.

    Plain Text lines rather than a Table. A Table.grid reflows its columns whenever
    their total exceeds the width it is rendered at -- and inside an OptionList that
    width is smaller than the widget's content_size -- so the six-wide avatar column
    was being shrunk and ellipsised into "...". Text with overflow="crop" cannot
    reflow: it either fits or is cut, which is the behaviour wanted here.
    """
    body_w = max(12, width - AVATAR_W)

    stamp = when(t["last"])
    name = pretty_addr(t["addr"])
    name_max = max(4, body_w - len(stamp) - 1)
    if len(name) > name_max:
        name = name[: name_max - 1] + "\u2026"

    head = Text(" " * AVATAR_W, no_wrap=True, overflow="crop")
    head.append(name, style="bold")
    head.append(" " * max(1, body_w - len(name) - len(stamp)))
    head.append(stamp, style="dim")

    preview = wrap(t["preview"], body_w, PREVIEW_LINES)
    preview += [""] * (PREVIEW_LINES - len(preview))

    # The avatar sits on the SECOND row, indented two columns: against a three-row
    # entry that reads as vertically centred, where top-left read as a bullet.
    av_row = Text(" " * AVATAR_INDENT, no_wrap=True, overflow="crop")
    av_row.append_text(avatar(t))
    # cell_len already counts the indent and the glyph, so pad to AVATAR_W outright --
    # subtracting the indent a second time left this row one column left of the others.
    av_row.append(" " * max(1, AVATAR_W - cell_len(av_row.plain)))
    av_row.append(preview[0], style="dim")

    tail = Text(" " * AVATAR_W, no_wrap=True, overflow="crop")
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
        border-right: solid $panel-darken-2;
        background: $surface;
    }
    #threads > .option-list--option { padding: 0; }
    #convo { width: 1fr; height: 1fr; padding: 0 1; }
    #status { height: auto; padding: 0 1; color: $text-muted; background: $panel; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "reload", "Reload"),
        Binding("g", "top", "Newest"),
        Binding("c", "codes_only", "2FA only"),
        Binding("d", "delete", "Delete chain"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.threads: list = []
        self.selected: str | None = None
        self.selected_thread: dict | None = None
        self.codes_only = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield OptionList(id="threads")
            # min_width defaults to 78: RichLog pads every rendered line out to at
            # least that, so in a narrower pane the right side was simply cut off.
            # That -- not the bubble geometry -- is why the reading pane appeared to
            # need ~82 columns.
            yield RichLog(id="convo", wrap=True, markup=False, highlight=False,
                          min_width=10)
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
        rows = store.threads()
        if self.codes_only:
            rows = [t for t in rows if t["codes"]]

        # Rebuild only on a real change: a poll every few seconds must not yank the
        # selection out from under someone who is reading.
        sig = [(t["addr"], t["count"], t["last"]) for t in rows]
        if sig == [(t["addr"], t["count"], t["last"]) for t in self.threads]:
            self.refresh_status()
            return

        self.threads = rows
        lv = self.query_one("#threads", OptionList)
        keep = self.selected
        lv.clear_options()
        width = self._list_width(lv)
        lv.add_options([Option(thread_row(t, width, i), id=self._key(t))
                        for i, t in enumerate(rows)])

        if rows:
            idx = next((i for i, t in enumerate(rows) if self._key(t) == keep), 0)
            lv.highlighted = idx
            self.show_thread(rows[idx])
        else:
            self.query_one("#convo", RichLog).clear()
            self.selected = None
        self.refresh_status()

    @staticmethod
    def _list_width(lv) -> int:
        """Columns actually available to a row.

        content_size excludes padding and border but NOT the scrollbar, which is
        always present here (301 conversations), so one more column comes off.
        """
        w = lv.content_size.width or lv.size.width or 44
        # An option renders NARROWER than the list's content_size: the border, the
        # scrollbar and OptionList's own per-option gutter all come off, and none of
        # them are visible in any public measurement. Six was measured, not reasoned:
        # render the app, look at whether a full timestamp survives, adjust. Redo it
        # with SMSTUI_SLACK if the stylesheet or Textual's option chrome changes.
        #
        # Erring large is safe -- rows are cropped, so an underestimate leaves a small
        # gap at the right. Erring small is what ate the avatars and the timestamps.
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
        bits = [f"{len(store.messages())} messages", f"{len(self.threads)} threads"]
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
        log = self.query_one("#convo", RichLog)
        log.clear()

        # Thread id is authoritative when we have one. Matching on aliases AS WELL was
        # the bug: an alias like "unknown" belongs to many unrelated conversations, so
        # opening a blocked junk thread pulled in every address-less message in the
        # archive. Aliases are only a fallback for records that predate the thread id.
        tid = t.get("thread")
        if tid is not None:
            msgs = [m for m in store.messages() if m.get("thread") == tid]
        else:
            aliases = set(t.get("addrs") or [t["addr"]])
            msgs = [m for m in store.messages()
                    if m.get("thread") is None and m.get("addr") in aliases]
        msgs = sorted(msgs, key=lambda m: m.get("rx", 0))
        # Only the tail: a 6,891-message conversation must not be rendered in full.
        if len(msgs) > SHOW_LAST:
            log.write(Text(f"... {len(msgs) - SHOW_LAST} earlier message(s) not shown",
                           style="dim"))
            msgs = msgs[-SHOW_LAST:]
        if not msgs:
            log.write(Text("no messages", style="dim"))
            return

        # content_size, not size: the latter includes padding, and reading it before
        # layout has run is what froze every bubble at a fixed 78 columns regardless
        # of the window. RichLog keeps rendered strips, so a width captured too early
        # is permanent -- hence the re-render after refresh in on_mount.
        pane_w = max(BUBBLE_MIN + 4,
                     (log.content_size.width or log.size.width or 60) - 1)
        bubble_w = max(BUBBLE_MIN, int(pane_w * BUBBLE_FRAC))
        log.min_width = min(10, pane_w)

        log.write(Text(pretty_addr(t["addr"]), style="bold"))
        log.write("")

        day = None
        for m in msgs:
            t = datetime.fromtimestamp(m.get("rx", 0))
            if t.date() != day:
                day = t.date()
                label = t.strftime("%A, %B %-d")
                if t.year != datetime.now().year:
                    label = t.strftime("%A, %B %-d, %Y")
                log.write(date_rule(label, pane_w))

            outgoing = m.get("dir") == "out"
            body = m.get("body") or "(no body stored)"
            label = t.strftime("%H:%M")
            if m.get("code"):
                label += f"  •  code {m['code']}"
            if m.get("kind") != "sms":
                label += f"  •  {m.get('kind')}"

            # Cap the bubble instead of letting it span the pane. expand=False only
            # hugs SHORT content; a long message still fills the full width and stops
            # looking like a message at all. Capping leaves the opposite margin
            # visible, which is what makes the left/right distinction readable.
            bubble = Panel(
                Text(body),
                title=label,
                title_align="right" if outgoing else "left",
                border_style="cyan" if outgoing else "green",
                expand=False,
                width=None if len(body) < bubble_w else bubble_w,
                padding=(0, 1),
            )
            if outgoing:
                # Measure, then pad: right alignment computed from the bubble's real
                # rendered width. No fixed indent -- a short message pins to the right
                # edge and a long one uses the full pane, so nothing is wasted.
                w = min(pane_w, Measurement.get(
                    _MEASURE, _MEASURE.options.update_width(pane_w), bubble).maximum)
                log.write(Padding(bubble, (0, 0, 0, max(0, pane_w - w))))
            else:
                log.write(bubble)

    # --------------------------------------------------------------- actions

    def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted) -> None:
        if 0 <= event.option_index < len(self.threads):
            self.show_thread(self.threads[event.option_index])

    def action_reload(self) -> None:
        self.threads = []
        self.reload()

    def action_top(self) -> None:
        if self.threads:
            self.query_one("#threads", OptionList).highlighted = 0

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

    def action_codes_only(self) -> None:
        self.codes_only = not self.codes_only
        self.threads = []
        self.reload()


def main() -> int:
    BridgeTUI().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
