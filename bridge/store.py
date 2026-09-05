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
PINS = DIR / "pins.jsonl"
# Content-addressed MMS parts. The same picture forwarded twice is stored once, and
# a retried upload cannot duplicate it.
ATTACH = DIR / "attachments"
# Touched by a desktop app while it is open (the TUI does it on every refresh). Its
# mtime is the "a human is at the desk" signal behind the phone's live link.
PRESENCE = DIR / "presence"
PRESENCE_TTL = 60

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
    "forward_threads":      ("threads",),    # -> ForwardMessageWorker for one conversation
    # Ask the phone for one image by digest. Backfill records digests without bytes:
    # a full history here is ~1,800 images and about 3 GB, almost none of it ever
    # looked at. Fetching the one you open costs seconds.
    "fetch_attachment":     ("sha", "message"),
    "send":                 ("addr", "body"),# -> SendNewMessage
    # Ask the phone where it is. The answer rides in the ACK's `result`:
    #   {"lat", "lon", "acc_m", "ts", "provider", "wifi_ssid", "wifi_bssid"}
    # any field null when unknown (no fix, not on wifi). The wifi pair is the
    # useful one: "phone is on the same access point as the box" is a perimeter
    # test with no range error at all. Read by agents/utils/location.py.
    "location":             (),
    # Diagnostics: what the phone decided about the live link and from what
    # (its wifi addresses, BSSID, the last hint it saw, the last error).
    "live_status":          (),
    # Put a message in front of the human as an entry in an SMS thread from
    # AGENT_ADDR: the phone INSERTS body into its inbox (no carrier involved), posts
    # its normal new-message notification (so car consoles, watches and Android Auto
    # see it like any text), and does NOT forward the inserted message back here.
    # AGENT_ADDR is not dialable, so a reply typed into that thread is never handed
    # to the radio; the phone POSTs it to /sms as dir=out, addr=AGENT_ADDR, which is
    # the human->agent path. Enqueued by agents/utils/notify.py.
    # args: body, and optionally addr (the agent's address, default chief@agents), name,
    # color, shape -- the identity the phone files the message under.
    "notify":               ("body",),
}

# The sender the phone shows for agent notifications. Alphanumeric on purpose: it
# cannot be a phone number, so nothing the human types into that thread can reach a
# carrier. The receiver also never extracts a code from this address, so an agent
# saying "confirmation 483920" cannot satisfy another agent's wait for a 2FA code.
AGENT_ADDR = "AGENTS"


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
_ids: dict = {}      # archive id -> message ts; an id seen again with a DIFFERENT ts is a reused id
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
        _ids = {r["id"]: r.get("ts") for r in _msgs if r.get("id")}
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

def resolve_id(mid: str, ts) -> tuple[str, bool]:
    """(archive id, already stored) for an incoming (id, ts).

    The phone's ids are not unique across time: its internal numbering restarts on a
    full resync, and the provider reuses row ids after deletions. A retry carries the
    same id AND the same ts; a reused id carries a different ts, and is stored under
    `<id>@<ts>` so both messages keep distinct archive ids.
    """
    _load()
    if not mid:
        return mid, False
    if mid not in _ids:
        return mid, False
    if ts is None or _ids[mid] == ts:
        return mid, True
    alt = f"{mid}@{ts}"
    return alt, alt in _ids


def add_message(rec: dict) -> str | None:
    """Append a message; returns the archive id it was stored under, or None if it was
    already there (a retry)."""
    aid, dup = resolve_id(rec.get("id"), rec.get("ts"))
    if dup:
        return None
    _append(ARCHIVE, {**rec, "id": aid} if aid != rec.get("id") else rec)
    return aid


def add_messages(recs: list) -> dict:
    """Bulk append for backfill. One pass, one fsync, dedup against the index.

    Backfill re-sends freely (a resumed run repeats its last batch), so duplicates
    are the normal case rather than an error -- they are counted, not rejected.
    """
    _load()
    fresh, dup, enriched = [], 0, {}
    stored = {r.get("id"): r for r in _msgs if r.get("id")}
    seen = set(_ids)
    # Content identity as well as id: the phone's id scheme has changed once already
    # (Realm ids, then provider row ids), and a fresh install backfills everything under
    # ids the archive has never seen. The same address, direction, second and text is
    # the same message whatever it is numbered. Observed 2026-09-05: 3,302 duplicates
    # in one run before this check existed.
    content = lambda r: (r.get("addr"), r.get("dir"), r.get("ts"), r.get("body") or "")
    held = {content(r) for r in _msgs if r.get("type") is None}
    for r in recs:
        mid = r.get("id")
        if mid and mid not in seen and content(r) in held:
            dup += 1
            continue
        if mid and mid in seen:
            dup += 1
            # A re-run that now carries attachments must be able to ENRICH a record
            # already held. Rejecting the duplicate wholesale uploaded the bytes and
            # then dropped the reference to them, so the images existed and nothing
            # pointed at them.
            old = stored.get(mid)
            # Enrich whenever the incoming parts carry MORE than the stored ones.
            # The first version only fired when the record had no parts at all, so a
            # record that already had digests could never gain thumbnails -- the
            # thumbnails uploaded and nothing referenced them, exactly the failure
            # this check was added to fix one level up.
            if r.get("parts") and old is not None and r["parts"] != old.get("parts"):
                merged = dict(old)
                merged["parts"] = r["parts"]
                enriched[mid] = merged
            continue
        if mid:
            seen.add(mid)
        held.add(content(r))
        fresh.append(r)

    if enriched:
        # One rewrite per batch rather than per record: the archive is append-only for
        # writes, and this is the one path that has to go back and amend.
        _rewrite(ARCHIVE, [enriched.get(r.get("id"), r) for r in _load()])

    if fresh:
        _ensure_dir()
        fd = os.open(ARCHIVE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with open(fd, "w") as f:
            for r in fresh:
                f.write(json.dumps(r) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return {"stored": len(fresh), "duplicates": dup, "enriched": len(enriched)}


def messages(since: int = 0, limit: int | None = None, addr: str | None = None,
             thread: int | None = None, addrs: list | None = None) -> list:
    """`addr` is an exact match; `thread`/`addrs` select a conversation the way threads()
    groups it (the phone's thread id where a record has one, the address otherwise)."""
    def wanted(r):
        if r.get("rx", 0) < since:
            return False
        if addr is not None and r.get("addr") != addr:
            return False
        if thread is not None or addrs:
            return (thread is not None and r.get("thread") == thread) or \
                   (bool(addrs) and r.get("addr") in addrs)
        return True
    # In time order, not file order: an agent's notify is recorded when the phone acks
    # it, which can land after a reply the human already typed to it.
    out = sorted((r for r in _load() if wanted(r)),
                 key=lambda r: (r.get("ts") or r.get("rx") or 0, r.get("rx") or 0))
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

    The same person arrives written several ways -- +18475550100, 18475550100,
    8475550100, and +18475550100@mms.example.net for an email-gatewayed MMS. Grouping on
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
                                    "preview": "", "codes": 0, "last_code": False, "thread": tid,
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
        # ts, not rx: the phone's message time is when it happened; rx is only when
        # this box heard about it, hours later if delivery was queued behind an
        # outage. The pre-v1 records have no ts, hence the fallback.
        mt = r.get("ts") or r.get("rx", 0)
        if mt >= t["last"]:
            t["last"] = mt
            t["preview"] = (r.get("body") or "")[:120]
            # The list shows a failed last send (a red badge, like the phone's list).
            t["last_failed"] = r.get("dir") == "out" and r.get("status") == "failed"
            t["last_out"] = r.get("dir") == "out"
        # A code is a per-message fact, so the key avatar follows the NEWEST inbound
        # message only: a dedicated 2FA sender never sends anything else and keeps the
        # key for good; a person or a shop that once sent a code loses it on their next
        # text. "codes" (lifetime count) stays for the code filter and history.
        if r.get("dir") != "out" and r.get("rx", 0) >= t.get("last_in", 0):
            t["last_in"] = r.get("rx", 0)
            t["last_code"] = bool(r.get("code"))

    from bridge import contacts
    out = []
    for t in groups.values():
        t["addr"] = display_addr(t["addrs"], t["in_addrs"])
        t["addrs"] = sorted(t["addrs"])
        t["in_addrs"] = sorted(t["in_addrs"])
        # A card name when the vdir has one for any address in the thread; None otherwise.
        t["name"] = contacts.name_for_any(t["in_addrs"]) or contacts.name_for_any(t["addrs"])
        t["photo"] = contacts.photo_for_any(t["in_addrs"]) or contacts.photo_for_any(t["addrs"])
        from bridge import agents as ag
        agent = ag.lookup(t["addr"]) if ag.is_agent(t["addr"]) else None
        if agent:
            t["name"] = agent["name"]
            t["agent"] = {k: agent[k] for k in ("id", "addr", "color", "shape")}
        elif t["addr"] == AGENT_ADDR:
            t["name"] = "Agents"          # the channel itself, from before agents had names
        out.append(t)
    cur_pins = pins()
    for t in out:
        p = cur_pins.get(verdict_key(t))
        t["pinned"] = bool(p and p["pinned"])
    # Pinned first, then newest. The pin bit comes from the same two-way sync the
    # block list rides: the phone's pins push here, and a TUI pin is commanded back.
    return sorted(out, key=lambda t: (not t["pinned"], -t["last"]))


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


def acked(op: str, limit: int = 20) -> list:
    """Applied commands of one op, each with its ack record, newest ack first."""
    cmds, acks = {}, {}
    for r in _read(COMMANDS):
        if r.get("type") == "cmd" and r.get("op") == op:
            cmds[r["id"]] = r
        elif r.get("type") == "ack":
            acks[r.get("id")] = r
    out = [{"cmd": c, "ack": acks[cid]} for cid, c in cmds.items() if cid in acks]
    return sorted(out, key=lambda x: x["ack"].get("at", 0), reverse=True)[:limit]


def latest_location() -> dict | None:
    """The newest location the phone reported, or None if it never has.

    `at` is when the ack landed here (box clock); `ts` inside is the fix time on
    the phone's clock. A phone that acked without a payload yields a dict with
    only `at` and `cmd`, which callers treat as "no fix"."""
    for x in acked("location", limit=1):
        res = x["ack"].get("result")
        res = res if isinstance(res, dict) else {}
        return {**res, "at": x["ack"].get("at"), "cmd": x["cmd"]["id"]}
    return None


# -------------------------------------------------------------------- presence / live link

def touch_presence(lan: list | None = None) -> None:
    """A desktop app is open. Call every few seconds while it is; stop when it closes.

    `lan` is the desktop's own IPv4 subnets, for a desktop on another machine (a laptop
    away from home): the phone opens the live link when it is on the same network as
    whichever desktop is open, not only this box. Kept in the presence file for as long
    as the presence itself; tailnet, loopback and link-local ranges are ignored."""
    _ensure_dir()
    nets = []
    for n in lan or ():
        try:
            import ipaddress
            net = ipaddress.ip_network(str(n), strict=False)
        except ValueError:
            continue
        if net.version != 4 or net.prefixlen >= 32 or net.is_loopback or net.is_link_local \
                or net.subnet_of(ipaddress.ip_network("100.64.0.0/10")):
            continue
        nets.append(str(net))
    PRESENCE.write_text(json.dumps({"lan": nets}))


def presence_lan() -> list:
    """Subnets the open desktop(s) reported, while the presence is fresh."""
    if not presence_active():
        return []
    try:
        return list(json.loads(PRESENCE.read_text() or "{}").get("lan") or [])
    except (OSError, ValueError):
        return []


def presence_active(ttl: int = PRESENCE_TTL) -> bool:
    try:
        return time.time() - PRESENCE.stat().st_mtime <= ttl
    except FileNotFoundError:
        return False


_bssid_cache: tuple[float, list] = (0.0, [])


def box_wifi_bssids() -> list:
    """BSSIDs this box is connected to (nmcli), cached for a minute. Empty when not on wifi."""
    global _bssid_cache
    if time.time() - _bssid_cache[0] < 60:
        return _bssid_cache[1]
    out = []
    try:
        import subprocess
        text = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,BSSID", "dev", "wifi", "list", "--rescan", "no"],
                              capture_output=True, text=True, timeout=10).stdout
        for line in text.splitlines():
            line = line.replace("\\:", "\0")
            parts = line.split(":")
            if len(parts) >= 2 and parts[0] == "yes":
                out.append(parts[1].replace("\0", ":").lower())
    except Exception:
        out = []
    _bssid_cache = (time.time(), out)
    return out


LIVE_SEEN = DIR / "live-seen"   # touched by each long-poll: "the phone's live link is up"


def mark_live_seen() -> None:
    _ensure_dir()
    LIVE_SEEN.touch()


def live_link_age() -> float | None:
    """Seconds since the phone last long-polled, or None if it never has."""
    try:
        return round(time.time() - LIVE_SEEN.stat().st_mtime, 1)
    except FileNotFoundError:
        return None


_lan_cache: tuple[float, list] = (0.0, [])


def box_lan_networks() -> list:
    """The IPv4 subnets this box sits on, as a.b.c.d/nn, tailscale excluded. A phone
    whose wifi address falls in one of them is on the same LAN -- no permission needed
    on the phone to know its own address, unlike the BSSID."""
    global _lan_cache
    if time.time() - _lan_cache[0] < 60:
        return _lan_cache[1]
    out = []
    try:
        import ipaddress, subprocess
        text = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                              capture_output=True, text=True, timeout=10).stdout
        for line in text.splitlines():
            f = line.split()
            if len(f) >= 4 and not f[1].startswith(("tailscale", "docker", "veth", "lo")):
                net = ipaddress.ip_interface(f[3]).network
                if net.prefixlen < 32:
                    out.append(str(net))
    except Exception:
        out = []
    _lan_cache = (time.time(), out)
    return out


def set_status(mid: str, status: str, ts=None) -> bool:
    """Update the delivery status of an outbound message already in the archive
    (the phone forwards a message again when it fails to send). Resolved by (id, ts)
    so a reused id can never touch an older message."""
    aid, known = resolve_id(mid, ts)
    if not known:
        return False
    changed = False
    recs = []
    for r in _read(ARCHIVE):
        if r.get("id") == aid and r.get("dir") == "out" and r.get("status") != status:
            r = {**r, "status": status}
            changed = True
        recs.append(r)
    if changed:
        _rewrite(ARCHIVE, recs)
    return changed


def live_hint() -> dict:
    """What the phone needs to decide on a live link: is anyone at the desk, and which
    access point counts as 'here'. Rides on every /commands and /sms response. `link`
    is for the desktop side: how long ago the phone's long-poll was last seen."""
    lan = box_lan_networks()
    lan += [n for n in presence_lan() if n not in lan]
    return {"wanted": presence_active(), "bssids": box_wifi_bssids(), "lan": lan,
            "ttl": PRESENCE_TTL, "link": live_link_age()}


# -------------------------------------------------------------------- agent instructions

def instructions(since: int = 0, limit: int | None = None, agent: str | None = None) -> list:
    """What the human typed into an agent's thread on the phone: outbound messages to an
    agent address, oldest first. `agent` narrows to one identity (id or address); the
    chief also receives what was typed into the legacy AGENTS thread. The bridge never
    interprets them; an agent subscribes with a watermark and reads its window."""
    from bridge import agents as ag
    if agent:
        ident = ag.identity(agent)
        addrs = {ident["addr"]} | ({AGENT_ADDR} if ident["addr"] == ag.CHIEF else set())
    else:
        addrs = None
    out = [m for m in messages(since=since) if m.get("dir") == "out" and ag.is_agent(m.get("addr"))
           and (addrs is None or (m.get("addr") or "") in addrs)]
    return out[-limit:] if limit else out


def _record_notify(cmd: dict, result) -> None:
    """An agent's message exists only on the phone unless we write it down here: the phone
    does not forward what the box itself wrote (that would loop), so the archive learns it
    at the ack, under the id the phone filed it as. Then the desktop and the TUI show the
    agent's side of the conversation too."""
    if not isinstance(result, dict) or not result.get("message"):
        return
    args = cmd.get("args") or {}
    add_message({"v": 1, "id": str(result["message"]), "dir": "in", "ts": int(time.time()),
                 "rx": int(time.time()), "addr": result.get("addr") or args.get("addr") or "chief@agents",
                 "body": args.get("body", ""), "kind": "sms", "sub": -1, "code": None,
                 "agent": True})


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
        if c["op"] == "notify":
            _record_notify(c, (results or {}).get(cid))
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


def set_pin(thread, addr: str, pinned: bool, source: str = "desktop") -> dict:
    """Record a pin decision. Same append-and-replay shape as verdicts, and the same
    source rules: "phone" entries follow the phone's complete pushed state, while a
    "desktop" entry stands until the phone confirms it (its mark_pinned/mark_unpinned
    command round-trips), so a pending pin cannot flicker off between drains."""
    rec = {"v": 1, "at": int(time.time()), "thread": thread,
           "addr": addr, "norm": normalize_addr(addr), "pinned": bool(pinned),
           "source": source}
    _append(PINS, rec)
    return rec


def pins() -> dict:
    """Current pin per conversation, by replaying the log. Keyed like verdicts."""
    out = {}
    for r in _read(PINS):
        key = ("t", r["thread"]) if r.get("thread") is not None else ("a", r.get("norm"))
        out[key] = r
    return out


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
