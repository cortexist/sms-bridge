"""Names for numbers, from a local vdir of vCards.

Radicale is the service; the directory this reads is only a cache of the same cards,
kept by vdirsyncer (vdirsyncer-contacts.timer). The bridge never writes it and never
talks CardDAV itself. Any vdir works, so a khard user is served by the same code.

Lookup is by the archive's own address normalisation, so a card's "(847) 555 0100",
a phone's "+18475550100" and a forwarded "8475550100" all meet at one key.
"""

import base64
import hashlib
import os
import time
from pathlib import Path

VDIR = Path(os.environ.get("SMS_CONTACTS_DIR", Path.home() / ".local" / "share" / "contacts"))

_cache: dict = {"stamp": None, "by_number": {}, "photo_by_number": {}, "cards": []}

# Contact photos, decoded out of the cards once per change so the desktop can show
# them from plain files (a QML Image cannot send the bridge's bearer token). Named by
# content hash: a card whose photo has not changed keeps its file and its URL.
def _photos_dir() -> Path:
    from bridge.store import DIR
    return DIR / "photos"


def _unfold(text: str) -> list:
    """vCard folds long lines with a leading space; join them back."""
    out = []
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _parse(text: str) -> dict | None:
    fn, n, tels, photo = "", "", [], None
    for line in _unfold(text):
        key, _, value = line.partition(":")
        name = key.split(";")[0].upper()
        if name == "PHOTO":
            photo = _photo_bytes(key, value)
        elif name == "FN":
            fn = value.strip()
        elif name == "N":
            parts = value.split(";")
            n = " ".join(p for p in (parts[1] if len(parts) > 1 else "", parts[0]) if p).strip()
        elif name == "TEL":
            v = value.strip()
            if v.startswith("tel:"):
                v = v[4:]
            if v:
                tels.append(v)
    display = fn or n
    if not display or not tels:
        return None
    return {"name": display, "numbers": tels, "photo": photo}


def _photo_bytes(key: str, value: str) -> bytes | None:
    """Inline photo data from a PHOTO line: vCard 3 `ENCODING=b`, vCard 4 `data:` URI.
    A URL (http...) is left alone; the desktop would need the network for it."""
    params = key.upper()
    v = value.strip()
    try:
        if "ENCODING=B" in params or "BASE64" in params:
            return base64.b64decode(v, validate=False) or None
        if v.lower().startswith("data:"):
            head, _, data = v.partition(",")
            if ";base64" in head.lower():
                return base64.b64decode(data, validate=False) or None
    except (ValueError, TypeError):
        return None
    return None


def _store_photo(data: bytes) -> str | None:
    """Write the bytes under their hash, return the path (str) or None on failure."""
    kind = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
    path = _photos_dir() / f"{hashlib.sha256(data).hexdigest()[:24]}.{kind}"
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return str(path)
    except OSError:
        return None


def _stamp():
    """Something that changes when any card changes: newest mtime and file count."""
    try:
        files = list(VDIR.rglob("*.vcf"))
    except OSError:
        return None
    return (len(files), max((f.stat().st_mtime for f in files), default=0))


def _load() -> None:
    from bridge.store import normalize_addr
    stamp = _stamp()
    if stamp == _cache["stamp"]:
        return
    cards, by_number, photo_by_number = [], {}, {}
    for f in sorted(VDIR.rglob("*.vcf")):
        try:
            card = _parse(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not card:
            continue
        photo = _store_photo(card["photo"]) if card.get("photo") else None
        card["photo"] = photo                       # the path now, never the bytes
        cards.append(card)
        for tel in card["numbers"]:
            key = normalize_addr(tel)
            if key and key not in by_number:
                by_number[key] = card["name"]
            if key and photo and key not in photo_by_number:
                photo_by_number[key] = photo
    _cache.update(stamp=stamp, cards=cards, by_number=by_number,
                  photo_by_number=photo_by_number, loaded=time.time())


def name_for(addr: str | None) -> str | None:
    """The card name for an address, or None. Cheap: the vdir is re-read only on change."""
    if not addr:
        return None
    from bridge.store import normalize_addr
    _load()
    return _cache["by_number"].get(normalize_addr(addr))


def name_for_any(addrs) -> str | None:
    for a in addrs or ():
        n = name_for(a)
        if n:
            return n
    return None


def photo_for_any(addrs) -> str | None:
    """URL path (`/photos/<name>`) of the card photo for the first address that has one,
    or None. Served by the bridge, so a desktop on another machine gets it too."""
    from bridge.store import normalize_addr
    _load()
    for a in addrs or ():
        p = _cache["photo_by_number"].get(normalize_addr(a)) if a else None
        if p:
            return "/photos/" + Path(p).name
    return None


def photo_file(name: str) -> Path | None:
    """The file behind `/photos/<name>`, or None: names are a hash plus an extension, and
    nothing else is ever looked up, so a path element cannot reach outside the cache."""
    import re
    if not re.fullmatch(r"[0-9a-f]{24}\.(jpg|png)", name or ""):
        return None
    p = _photos_dir() / name
    return p if p.is_file() else None


def all_cards() -> list:
    _load()
    return list(_cache["cards"])
