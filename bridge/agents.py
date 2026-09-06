"""The agents' identities, from the `agents` address book cached beside the contacts.

Each agent on the box is a vCard in Radicale's `agents` collection -- a trackable entity
with a name, a virtual SMS address, an avatar expressed as two parameters, and optionally
a mailbox -- because an agent cannot act for a real person without being one. Custom fields:

    X-SMS-ADDRESS   ides@agents      the thread the human talks to it in; never dialable
                                     (the part after @ must be the configured DOMAIN)
    X-AGENT-COLOR   #9ece6a          avatar colour
    X-AGENT-SHAPE   0..3             avatar shape (square, rounded, round, squircle)

Standard FN, EMAIL, PHOTO and NOTE are used as they are: with a PHOTO on the card the
phone and the desktop show it instead of the drawn face (colour still tints the bubbles). The chief (chief@agents) is the
front desk: any agent without a card speaks through it. "AGENTS" is the legacy channel
the chief reads, not an alias for the chief.
"""

import os
from pathlib import Path

from bridge import contacts

VDIR = Path(os.environ.get("SMS_AGENTS_DIR", contacts.VDIR / "agents"))
# The domain of every agent address (x@<domain>). "agents" by default; set
# SMS_AGENTS_DOMAIN to one you own (agents.example.net) so no outside sender can
# collide with it. The pairing code carries it to the phone, /agents to the desktop.
DOMAIN = (os.environ.get("SMS_AGENTS_DOMAIN", "agents").strip().lstrip("@").lower()) or "agents"
CHIEF = f"chief@{DOMAIN}"
LEGACY = "AGENTS"

_cache: dict = {"stamp": None, "agents": []}


def is_agent(addr: str | None) -> bool:
    a = (addr or "").strip().lower()
    return a == LEGACY.lower() or a.endswith("@" + DOMAIN)


def _parse(text: str) -> dict | None:
    fn, addr, color, shape, email, note, photo = "", "", "", None, "", "", None
    for line in contacts._unfold(text):
        key, _, value = line.partition(":")
        name = key.split(";")[0].upper()
        v = value.strip()
        if name == "FN": fn = v
        elif name == "PHOTO": photo = contacts._photo_bytes(key, value)
        elif name == "X-SMS-ADDRESS": addr = v.lower()
        elif name == "X-AGENT-COLOR": color = v
        elif name == "X-AGENT-SHAPE": shape = int(v) if v.isdigit() else None
        elif name == "EMAIL" and not email: email = v
        elif name == "NOTE": note = v.replace("\\n", "\n").replace("\\,", ",")
    if not fn or not addr:
        return None
    path = contacts._store_photo(photo) if photo else None
    return {"id": addr.split("@")[0], "addr": addr, "name": fn, "color": color or "#7aa2f7",
            "shape": shape if shape is not None else 0, "email": email, "note": note,
            "photo": ("/photos/" + Path(path).name) if path else None}


def photo_data_uri(agent: dict | None) -> str | None:
    """The agent's card photo as a data URI, for the notify command: the phone keeps it
    beside the name and colour and shows it as the avatar. Card photos are small (a few
    kilobytes); anything over 64 KB is left out rather than bloating every notify."""
    if not agent or not agent.get("photo"):
        return None
    f = contacts.photo_file(agent["photo"].rsplit("/", 1)[-1])
    if f is None:
        return None
    data = f.read_bytes()
    if len(data) > 65536:
        return None
    import base64
    mime = "image/png" if f.suffix == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _load() -> None:
    try:
        files = sorted(VDIR.glob("*.vcf"))
        stamp = (len(files), max((f.stat().st_mtime for f in files), default=0))
    except OSError:
        files, stamp = [], None
    if stamp == _cache["stamp"]:
        return
    agents = []
    for f in files:
        try:
            card = _parse(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if card:
            agents.append(card)
    _cache.update(stamp=stamp, agents=agents)


def all_agents() -> list:
    _load()
    return list(_cache["agents"])


def lookup(key: str | None) -> dict | None:
    """By short id ("ides") or address ("ides@agents"). The legacy AGENTS address is a
    channel, not an agent: it names the function (everyone's front desk), while Chief
    is the entity that reads it, at chief@agents. So it resolves to no identity."""
    if not key:
        return None
    k = key.strip().lower()
    if k == LEGACY.lower():
        return None
    _load()
    for a in _cache["agents"]:
        if k in (a["id"], a["addr"]):
            return a
    return None


def chief() -> dict:
    return lookup(CHIEF) or {"id": "chief", "addr": CHIEF, "name": "Chief", "color": "#7aa2f7", "shape": 0, "email": "", "note": ""}


def identity(key: str | None) -> dict:
    """The identity a message should be sent under: the agent's own card, else the chief."""
    return lookup(key) or chief()
