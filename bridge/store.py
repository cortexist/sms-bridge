"""Storage for the sms-bridge: the message archive and the outbound command queue.

Shared by the HTTP receiver and the TUI, which is why it is a module rather than
more code inside the server script.

APPEND-ONLY, fsync per record, for both logs. This box silent-resets every few
hours (~/co-tune/SILENT-RESET.md), so a torn trailing line is an expected event
rather than corruption: readers skip an unparseable line and everything before it
is still good. There is no index to rebuild and no recovery step.

TWO LOGS
    messages.jsonl   the archive: every SMS the phone forwarded
    commands.jsonl   the command queue: what the desktop wants the phone to do

COMMANDS ARE EVENTS, NOT MUTABLE ROWS. A command is appended once; its
acknowledgement is appended later as a separate record, and current state is
derived by replaying the log. That keeps the file append-only while still having
mutable-looking status, and it means an interrupted ack cannot corrupt anything --
at worst the command is re-sent, which is why every op must be idempotent.

THE PHONE IS WHERE THINGS HAPPEN. The desktop does not delete a message; it asks
the phone to, and the phone owns the Telephony provider that actually holds it.
Same shape as IMAP: the client issues the command, the store executes it. So a
command is `pending` until the phone acks, and the TUI shows that rather than
pretending the change already landed.

ARCHIVE DELETION IS DEFERRED TO THE ACK. A delete only leaves messages.jsonl once
the phone confirms it actually happened, so the two sides cannot silently diverge.
"""

import json
import os
import re
import time
import uuid
from pathlib import Path

DIR = Path(os.environ.get("SMS_BRIDGE_DIR", Path.home() / ".sms2fa"))
ARCHIVE = DIR / "messages.jsonl"
COMMANDS = DIR / "commands.jsonl"
CODE_VIEW = DIR / "latest.json"
# Quarantine verdicts, append-only like everything else. Each entry records who
# decided -- "auto", "desktop", "phone" -- because the corrections are the labels
# the rules have to learn from, and an auto verdict a human overrode is the most
# valuable record in the file.
VERDICTS = DIR / "quarantine.jsonl"
# Content-addressed MMS parts. The same picture forwarded twice is stored once, and
# a retried upload cannot duplicate it.
ATTACH = DIR / "attachments"

# The command vocabulary is QUIK's interactor set, deliberately. The rule is that a
# button in the TUI does what the identically-labelled button in the app does -- so
# there are no bespoke semantics to learn, and "Block" is app-local on both ends
# because that is what QUIK's block honestly is (a per-install Realm list, which is
# why blocks do not survive switching SMS apps). To make spam actually go away, use
# Delete; Block only stops future notifications inside one install.
OPS = {
    # In the protocol but NOT offered by the TUI. Deleting a named message means
    # deleting from the middle of a chain, which we do not do -- and the archive is
    # only what has been forwarded, with no backfill, so this side cannot even tell
    # where a chain starts. Kept because the phone implements it and a future
    # backfilled archive could use it honestly.
    "delete_messages":      ("ids",),        # -> DeleteMessages       (real: contentResolver.delete)
    # Addressable by phone threadId or by address: the desktop groups the archive by
    # address, and older forwarded records predate the `thread` field entirely, so the
    # phone resolves an address to its conversation when applying.
    "delete_conversations": (),              # -> DeleteConversations
    "mark_read":            ("threads",),    # -> MarkRead
    "mark_unread":          ("threads",),    # -> MarkUnread
    "mark_archived":        ("threads",),    # -> MarkArchived
    "mark_unarchived":      ("threads",),    # -> MarkUnarchived
    "mark_blocked":         ("threads",),    # -> MarkBlocked          (app-local)
    "mark_unblocked":       ("threads",),    # -> MarkUnblocked
    "mark_pinned":          ("threads",),    # -> MarkPinned
    "mark_unpinned":        ("threads",),    # -> MarkUnpinned
    # FIFO trimming, expressed by age rather than by id. Evaluated ON THE PHONE
    # against its complete chain, which is the only place the true order is known;
    # computing "oldest N" from this side's partial archive would silently delete
    # from the middle of the real chain.
    "delete_old_messages":  ("days",),       # -> deleteOldMessages(maxAgeDays)
    # Pull the phone's existing history into this archive. The phone owns paging and
    # its own resume cursor; this is just the go signal.
    "backfill":             (),              # -> BackfillWorker
    "send":                 ("addr", "body"),# -> SendNewMessage
}


# Two caches, both keyed on the archive's (mtime, size). At three records nothing
# here mattered; at ten thousand it decides whether the tool is usable.
#
#   _ids     dedup used to rescan the whole file per inserted message -- O(n^2) over
#            a backfill, ~50M comparisons for a 10k archive.
#   _msgs    the TUI re-reads and re-parses every record every few seconds; without
#            this that is 10k JSON parses per refresh.
#
# Safe because the server process is the only writer of messages; the TUI only ever
# appends commands. Any rewrite (deletion, prune) invalidates by changing mtime/size.
_cache_key = None
_ids: set = set()
_msgs: list = []


def _stat_key():
    try:
        st = ARCHIVE.stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


def _load() -> list:
    """Archive records, reparsed only when the file actually changed."""
    global _cache_key, _ids, _msgs
    key = _stat_key()
    if key != _cache_key:
        _msgs = list(_read(ARCHIVE))
        _ids = {r.get("id") for r in _msgs if r.get("id")}
        _cache_key = key
    return _msgs


# ------------------------------------------------------------------ primitives

def _ensure_dir() -> None:
    DIR.mkdir(mode=0o700, parents=True, exist_ok=True)


def _append(path: Path, rec: dict) -> None:
    _ensure_dir()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with open(fd, "w") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _read(path: Path):
    """Yield records, skipping any torn trailing line from a hard reset."""
    try:
        with open(path) as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except FileNotFoundError:
        return


def _rewrite(path: Path, records) -> None:
    """Replace a log atomically. Used only for deletion and pruning."""
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# -------------------------------------------------------------------- messages

def add_message(rec: dict) -> bool:
    """Append a message. False if its id was already stored (a retry)."""
    _load()
    mid = rec.get("id")
    if mid and mid in _ids:
        return False
    _append(ARCHIVE, rec)
    return True


def add_messages(recs: list) -> dict:
    """Bulk append for backfill. One pass, one fsync, dedup against the index.

    Backfill re-sends freely (a resumed run repeats its last batch), so duplicates
    are the normal case rather than an error -- they are counted, not rejected.
    """
    _load()
    fresh, dup = [], 0
    seen = set(_ids)
    for r in recs:
        mid = r.get("id")
        if mid and mid in seen:
            dup += 1
            continue
        if mid:
            seen.add(mid)
        fresh.append(r)
    if fresh:
        _ensure_dir()
        fd = os.open(ARCHIVE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with open(fd, "w") as f:
            for r in fresh:
                f.write(json.dumps(r) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return {"stored": len(fresh), "duplicates": dup}


def messages(since: int = 0, limit: int | None = None, addr: str | None = None) -> list:
    out = [r for r in _load()
           if r.get("rx", 0) >= since and (addr is None or r.get("addr") == addr)]
    return out[-limit:] if limit else out


# Android writes this in place of a recipient on outgoing MMS. It is a placeholder,
# never an address, and must not reach the UI or be used as a grouping key.
PLACEHOLDER_ADDRS = {"insert-address-token", "", "unknown"}


def display_addr(addrs, incoming=()) -> str:
    """The best spelling of an address to show, given every variant seen in a thread.

    Prefers an address seen on an INCOMING message. On an outgoing MMS Android records
    the sender -- your own number -- as the address, so without this a thread could be
    labelled with your own number instead of the person you were talking to. It ties
    with theirs on length, and max() breaks ties by order, so it won.

    Then: something that looks like a number, and the fullest spelling of it.
    """
    def usable(seq):
        return [a for a in seq if a not in PLACEHOLDER_ADDRS
                and sum(c.isdigit() for c in a) >= 3]

    return (max(usable(incoming), key=len) if usable(incoming)
            else max(usable(addrs), key=len) if usable(addrs)
            else (sorted(addrs)[0] if addrs else "?"))


def normalize_addr(a: str) -> str:
    """Grouping key for an address.

    The same person arrives written several ways -- +18472261218, 18472261218,
    8472261218, and +15614097922@one.att.net for an email-gatewayed MMS. Grouping on
    the raw string split one contact across three list entries in the backfill.
    """
    a = (a or "").split("@")[0]                 # email-gatewayed MMS
    d = "".join(c for c in a if c.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) >= 7 else (a or "?")     # shortcodes stay as written


def threads() -> list:
    """Conversations, newest first, for the TUI list pane.

    Keyed by the phone's threadId where we have one, since that is the identity the
    phone itself uses; a normalised address otherwise. Backfilled records carry a
    thread id and pre-backfill ones do not, so addresses are mapped to their thread
    first and the two sets are then merged -- otherwise the same conversation would
    appear twice, split by when the bridge happened to learn about it.
    """
    msgs = messages()

    addr_to_thread: dict[str, int] = {}
    for r in msgs:
        if r.get("thread") is not None:
            addr_to_thread.setdefault(normalize_addr(r.get("addr")), r["thread"])

    groups: dict = {}
    for r in msgs:
        norm = normalize_addr(r.get("addr"))
        tid = r.get("thread")
        if tid is None:
            tid = addr_to_thread.get(norm)
        key = ("t", tid) if tid is not None else ("a", norm)

        t = groups.setdefault(key, {"addr": r.get("addr") or "?", "count": 0, "last": 0,
                                    "preview": "", "codes": 0, "thread": tid,
                                    "norm": norm, "addrs": set(), "in_addrs": set()})
        t["count"] += 1
        a = r.get("addr") or ""
        # Placeholders are never aliases. "unknown" in particular appears in many
        # unrelated threads, so treating it as one made it a wildcard that matched
        # every address-less message in the archive.
        if a not in PLACEHOLDER_ADDRS:
            t["addrs"].add(a)
            if r.get("dir") != "out":
                t["in_addrs"].add(a)
        if r.get("code"):
            t["codes"] += 1
        if r.get("rx", 0) >= t["last"]:
            t["last"] = r.get("rx", 0)
            t["preview"] = (r.get("body") or "")[:120]

    out = []
    for t in groups.values():
        t["addr"] = display_addr(t["addrs"], t["in_addrs"])
        t["addrs"] = sorted(t["addrs"])
        t["in_addrs"] = sorted(t["in_addrs"])
        out.append(t)
    return sorted(out, key=lambda t: t["last"], reverse=True)


def code_since(since: int, ttl: int | None = None) -> dict | None:
    """Newest 2FA code that arrived after `since`. Closes an agent's login loop."""
    best = None
    for r in messages(since=since):
        if r.get("code"):
            best = r
    if not best:
        return None
    age = int(time.time()) - best.get("rx", 0)
    if ttl is not None and age > ttl:
        return None
    return {"code": best["code"], "from": best.get("addr", "unknown"),
            "received": best.get("rx"), "age_seconds": age}


def _forget(ids: set = frozenset(), addrs: set = frozenset(),
            threads: set = frozenset(), older_than: int | None = None) -> int:
    """Drop messages from the archive. Called on ack, never before.

    Deleting a conversation removes the whole chain, matching what the phone did:
    QUIK's delete acts on the conversation, so leaving orphaned messages here would
    make the two sides disagree the moment the command succeeded.
    """
    if not (ids or addrs or threads or older_than):
        return 0
    kept, dropped = [], 0
    for r in _load():
        if (r.get("id") in ids or r.get("addr") in addrs
                or (r.get("thread") is not None and r.get("thread") in threads)
                or (older_than is not None and r.get("ts", r.get("rx", 0)) < older_than)):
            dropped += 1
        else:
            kept.append(r)
    if dropped:
        _rewrite(ARCHIVE, kept)
    return dropped


# -------------------------------------------------------------------- commands

def enqueue(op: str, **args) -> dict:
    if op not in OPS:
        raise ValueError(f"unknown op {op!r}; known: {', '.join(sorted(OPS))}")
    missing = [k for k in OPS[op] if k not in args]
    if missing:
        raise ValueError(f"op {op!r} needs {', '.join(missing)}")
    if op == "delete_conversations" and not (args.get("addrs") or args.get("threads")):
        # No fixed arg list, so an empty call would otherwise enqueue a no-op that
        # looks like a real pending delete.
        raise ValueError("delete_conversations needs addrs and/or threads")
    cmd = {"v": 1, "type": "cmd", "id": f"cmd-{uuid.uuid4().hex[:12]}",
           "op": op, "args": args, "created": int(time.time())}
    _append(COMMANDS, cmd)
    return cmd


def _replay() -> tuple[dict, set]:
    cmds, acked = {}, set()
    for r in _read(COMMANDS):
        if r.get("type") == "cmd":
            cmds[r["id"]] = r
        elif r.get("type") == "ack":
            acked.add(r.get("id"))
    return cmds, acked


def pending(limit: int = 50) -> list:
    cmds, acked = _replay()
    out = [c for cid, c in cmds.items() if cid not in acked]
    return sorted(out, key=lambda c: c["created"])[:limit]


def all_commands() -> list:
    """Every command with a derived status, newest first -- for the TUI."""
    cmds, acked = _replay()
    out = []
    for cid, c in cmds.items():
        d = dict(c)
        d["status"] = "applied" if cid in acked else "pending"
        out.append(d)
    return sorted(out, key=lambda c: c["created"], reverse=True)


def ack(ids, results: dict | None = None) -> dict:
    """Mark commands applied. Archive deletion happens HERE, not at enqueue.

    Deferring the archive edit to the ack is what keeps the two sides consistent:
    if the phone never applies the delete, the message is still in the archive and
    the command is still pending, which is the truth.
    """
    cmds, already = _replay()
    acked = []
    ids_, addrs_, threads_ = set(), set(), set()
    older_than = None
    for cid in ids:
        if cid in already or cid not in cmds:
            continue                     # idempotent: re-acking is a no-op
        c = cmds[cid]
        _append(COMMANDS, {"v": 1, "type": "ack", "id": cid, "at": int(time.time()),
                           "result": (results or {}).get(cid)})
        acked.append(cid)
        if c["op"] == "delete_messages":
            ids_.update(c["args"].get("ids") or [])
        elif c["op"] == "delete_conversations":
            addrs_.update(c["args"].get("addrs") or [])
            threads_.update(c["args"].get("threads") or [])
        elif c["op"] == "delete_old_messages":
            older_than = min(older_than, int(time.time()) - int(c["args"]["days"]) * 86400)
    return {"acked": acked,
            "archive_removed": _forget(ids_, addrs_, threads_, older_than)}


# ------------------------------------------------------------------ quarantine

def set_verdict(thread, addr: str, junk: bool, source: str = "desktop",
                reasons=None, score: int = 0) -> dict:
    """Record a quarantine decision. Later entries win; history is kept."""
    rec = {"v": 1, "at": int(time.time()), "thread": thread,
           "addr": addr, "norm": normalize_addr(addr), "junk": bool(junk),
           "source": source, "reasons": reasons or [], "score": score}
    _append(VERDICTS, rec)
    return rec


def verdicts() -> dict:
    """Current verdict per conversation, by replaying the log.

    Keyed on thread id where there is one, normalised address otherwise -- the same
    identity rule the thread list uses, so a verdict survives a thread being
    relabelled or re-forwarded.
    """
    out = {}
    for r in _read(VERDICTS):
        key = ("t", r["thread"]) if r.get("thread") is not None else ("a", r.get("norm"))
        out[key] = r
    return out


def verdict_key(t: dict):
    return ("t", t["thread"]) if t.get("thread") is not None else ("a", t.get("norm"))


def labels() -> dict:
    """Human corrections only: the training set the rules do not have yet.

    An auto verdict later overridden by a person is the interesting case, so both
    the original and the correction are returned.
    """
    history, human = {}, {}
    for r in _read(VERDICTS):
        key = ("t", r["thread"]) if r.get("thread") is not None else ("a", r.get("norm"))
        if r.get("source") == "auto":
            history[key] = r
        else:
            human[key] = {"label": r, "overrode": history.get(key)}
    return human


# ----------------------------------------------------------------- attachments

def attachment_path(sha: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        raise ValueError("not a sha256")          # never let a path element in
    return ATTACH / sha


def put_attachment(sha: str, data: bytes) -> bool:
    """Store a part. False if it was already held -- content addressing makes a
    duplicate upload a no-op rather than a conflict."""
    p = attachment_path(sha)
    if p.exists():
        return False
    ATTACH.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o600)
    tmp.replace(p)
    return True


def has_attachment(sha: str) -> bool:
    try:
        return attachment_path(sha).exists()
    except ValueError:
        return False
