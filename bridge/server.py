"""sms-bridge HTTP endpoint: receives forwarded messages, hands back commands.

The phone POSTs every message here and this end stores it, derives any 2FA code,
and -- the new half -- answers with whatever the desktop has queued for the phone
to do. Piggybacking the command list on the forward response means the common case
costs no extra round trip at all: messages flowing in is exactly when commands
flow out.

There is deliberately no listener on the phone. An inbound socket there would need
a foreground service to survive Doze, which means a permanent notification and
battery cost, to save latency on operations (delete, block, archive) where nobody
is waiting. The phone polls; the desktop queues.

Bound to the Tailscale address only, so the socket is not on whatever wifi the box
is sitting on and every byte crosses WireGuard. A bearer token is still required:
the tailnet is small but it is not a trust boundary for an endpoint that hands out
login codes, and the token means a misconfigured bind cannot silently become a
public code-injection endpoint.

SECURITY NOTE, unchanged and still true: this holds every message you receive,
indefinitely, on a filesystem with no LUKS. The code TTL bounds /latest and /code;
the archive itself does not expire. Deliberate deletes now prune it (a TUI delete
removes the row on ack), which helps, but is not the same as encryption.
"""

import hashlib
import hmac
import http.server
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge import junk, store  # noqa: E402

TOKEN = (store.DIR / "token").read_text().strip()
BIND = os.environ.get("SMS2FA_BIND", "100.99.132.67")
PORT = int(os.environ.get("SMS2FA_PORT", "8090"))
TTL = int(os.environ.get("SMS2FA_TTL", "600"))
BODIES = os.environ.get("SMSBRIDGE_STORE_BODIES", "1") != "0"
# 256 KiB is right for a single forwarded message; a backfill batch is a legitimate
# large payload, so the cap is per-endpoint rather than global.
MAXBODY = 262144
MAXBULK = 8 * 1024 * 1024

# A CODE NEEDS A CODE WORD. The earlier fallback -- "any standalone 4-8 digit run" --
# keyed 150 of 303 threads: years ("07/29/2026"), street numbers ("3707 S Oak Knoll"),
# a port ("localhost:5000", 161 times), the tail of a phone number in a voicemail
# transcript, a payment amount. Those reached the clipboard and the key avatar alike.
# Digits alone say nothing; junk.looks_like_code already held this rule, extract() did not.
#
# Ordered most- to least-specific: digits right after a code word, digits right before
# "is your", then any standalone 6-8 digit run provided a code word appears somewhere in
# the message (a 4-5 digit run with the code word far away is still more often a year
# or a house number than a code).
CODEWORD = re.compile(r"\b(code|otp|passcode|verification|verify|2fa|one[- ]time|pin)\b", re.I)
PATTERNS = [
    re.compile(r"(?:code|otp|pin|passcode|verification|verify|2fa|one[- ]time)\W{0,3}"
               r"(?:is|:|-|—)?\W{0,3}(?:[A-Z]-)?(\d{4,8})\b", re.I),
    re.compile(r"(?:^|\W)(?:[A-Z]-)?(\d{4,8})\W{0,3}(?:is your|as your|to (?:confirm|verify|log))", re.I),
    # A trailing "." or "," is only disqualifying when a digit follows it (a decimal or a
    # thousands group); "...sign-in is 557762." is a code ending a sentence.
    re.compile(r"(?<![\d$#*:.,/-])(\d{6,8})(?!\d|[%/-]|[.,]\d)"),
]
YEAR = re.compile(r"^(19|20)\d\d$")


def extract(body: str):
    text = body or ""
    if not CODEWORD.search(text):
        return None
    for p in PATTERNS:
        for m in p.finditer(text):
            digits = m.group(1)
            if len(digits) == 4 and YEAR.match(digits):
                continue                      # "expires 2026" is not a code
            return digits
    return None


def notify(code: str, sender: str) -> None:
    # Best effort: the user manager may have no Wayland session, and a missing
    # notification must never make the receiver drop a code.
    env = dict(os.environ)
    env.setdefault("WAYLAND_DISPLAY", "wayland-1")
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    for cmd in (["wl-copy", "--", code],
                ["notify-send", "-u", "critical", "-t", "60000",
                 f"2FA code: {code}", f"from {sender} - copied to clipboard"]):
        try:
            subprocess.run(cmd, env=env, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def current_code():
    try:
        rec = json.loads(store.CODE_VIEW.read_text())
    except Exception:
        return None
    age = int(time.time()) - int(rec.get("received", 0))
    if age > TTL:
        return None
    rec["age_seconds"] = age
    return rec


class H(http.server.BaseHTTPRequestHandler):
    server_version = "sms-bridge"

    def handle_one_request(self):
        """A phone that times out mid-upload resets the connection; that is routine
        on a mobile link and should not print a traceback per occurrence."""
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def _auth(self) -> bool:
        # Constant-time: the only thing between the tailnet and an endpoint that
        # hands out login codes.
        return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + TOKEN)

    def _reply(self, code: int, obj=None) -> None:
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json_body(self, cap: int = MAXBODY):
        n = int(self.headers.get("Content-Length", 0))
        if n > cap:
            return None, self._reply(413, {"error": "too large"})
        try:
            return json.loads(self.rfile.read(n) or b"{}"), None
        except Exception:
            return None, self._reply(400, {"error": "bad json"})

    # ------------------------------------------------------------------ POST

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if not self._auth():
            return self._reply(401, {"error": "unauthorized"})
        if path == "/sms":
            return self._ingest()
        if path == "/commands/ack":
            return self._ack()
        if path == "/messages/bulk":
            return self._bulk()
        if path == "/quarantine":
            return self._quarantine()
        if path == "/blocked":
            return self._blocked()
        if path.startswith("/attachments/"):
            return self._put_attachment(path.rsplit("/", 1)[-1])
        return self._reply(404, {"error": "not found"})

    def _ingest(self):
        msg, err = self._json_body()
        if msg is None:
            return err

        now = int(time.time())
        # Accept the v1 shape and the original {from, body} shape alike.
        body = str(msg.get("body", ""))
        sender = str(msg.get("addr") or msg.get("from") or "unknown")
        mid = msg.get("id") or f"legacy:{now}:{hash(body) & 0xffffffff:08x}"
        code = extract(body)

        rec = {"v": 1, "id": str(mid), "dir": str(msg.get("dir", "in")),
               "ts": int(msg.get("ts", now)), "rx": now, "addr": sender,
               "kind": str(msg.get("kind", "sms")), "sub": int(msg.get("sub", -1)),
               "code": code}
        if msg.get("thread") is not None:
            rec["thread"] = msg["thread"]
        if isinstance(msg.get("parts"), list) and msg["parts"]:
            rec["parts"] = msg["parts"]
        if BODIES:
            rec["body"] = body

        fresh = store.add_message(rec)

        if code and fresh:
            tmp = store.CODE_VIEW.with_suffix(".tmp")
            tmp.write_text(json.dumps({"code": code, "from": sender, "received": now}))
            os.chmod(tmp, 0o600)
            os.replace(tmp, store.CODE_VIEW)
            notify(code, sender)

        # Never log the code or the body.
        print(f"{'stored' if fresh else 'duplicate'} from {sender}"
              f"{' (code)' if code else ''}", flush=True)

        if fresh:
            self._auto_classify(rec)

        # Piggyback: the response carries whatever the desktop queued.
        return self._reply(200, {"ok": True, "commands": store.pending()})

    def _auto_classify(self, rec: dict) -> None:
        """Score the thread a new message landed in.

        Only ever writes an `auto` verdict, and only when nobody has ruled on the
        conversation already: a human decision must never be overwritten by the
        rules, or the corrections that make up the training set would evaporate.
        """
        key = ("t", rec["thread"]) if rec.get("thread") is not None \
            else ("a", store.normalize_addr(rec["addr"]))
        existing = store.verdicts().get(key)
        if existing and existing.get("source") != "auto":
            return

        tid = rec.get("thread")
        convo = [m for m in store.messages()
                 if (tid is not None and m.get("thread") == tid)
                 or (tid is None and store.normalize_addr(m.get("addr", ""))
                     == store.normalize_addr(rec["addr"]))]
        v = junk.classify(convo)
        if v["junk"] and not (existing and existing["junk"]):
            store.set_verdict(tid, rec["addr"], True, "auto", v["reasons"], v["score"])
            print(f"quarantined {rec['addr']} ({', '.join(v['reasons'])})", flush=True)

    def _put_attachment(self, sha: str):
        """Raw bytes, content-addressed. Not JSON: an MMS image base64'd into a JSON
        body would be a third larger for no benefit."""
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0 or n > MAXBULK:
            return self._reply(413, {"error": "bad length"})
        data = self.rfile.read(n)
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha:
            # The name IS the integrity check; trusting the client's label would let
            # a bad upload poison every message referencing that digest.
            return self._reply(400, {"error": "digest mismatch"})
        try:
            fresh = store.put_attachment(sha, data)
        except ValueError:
            return self._reply(400, {"error": "bad digest"})
        print(f"attachment {sha[:12]} {'stored' if fresh else 'already held'} "
              f"({len(data)} bytes)", flush=True)
        return self._reply(200 if fresh else 409, {"ok": True, "stored": fresh})

    def _quarantine(self):
        """Desktop verdict: {thread, addr, junk}."""
        obj, err = self._json_body()
        if obj is None:
            return err
        rec = store.set_verdict(obj.get("thread"), str(obj.get("addr", "")),
                                bool(obj.get("junk")),
                                str(obj.get("source", "desktop")))
        return self._reply(200, {"ok": True, "verdict": rec})

    def _blocked(self):
        """The phone's own block list, so marking junk there lands here too.

        QUIK's block is app-local and reversible -- a quarantine in all but name --
        which is why it can carry this without any new UI on the phone.
        """
        obj, err = self._json_body(cap=MAXBULK)
        if obj is None:
            return err
        addrs = obj.get("addrs") or []
        if not isinstance(addrs, list):
            return self._reply(400, {"error": "addrs must be a list"})
        # Newer phones also send the blocked conversations with their thread ids --
        # the identity verdicts are keyed on -- and treat the payload as the phone's
        # COMPLETE block state, so a phone unblock releases here too. Older payloads
        # (no "blocked" field) stay add-only: an absent list must not wipe verdicts.
        blocked = obj.get("blocked")
        complete = isinstance(blocked, list)
        if not complete:
            blocked = []

        # thread id per address, so a phone verdict keys the same way a desktop one does
        thread_of = {}
        for t in store.threads():
            for a in t["addrs"]:
                thread_of[store.normalize_addr(a)] = t["thread"]

        current = store.verdicts()
        added = released = 0
        reported = set()

        def flag(tid, a):
            nonlocal added
            key = ("t", tid) if tid is not None else ("a", store.normalize_addr(a))
            reported.add(key)
            was = current.get(key)
            if was and was["junk"] and was.get("source") == "phone":
                return                        # already recorded; keep the log quiet
            if was and was.get("source") == "desktop" and not was["junk"]:
                return                        # desktop released it; do not re-flag
            store.set_verdict(tid, a, True, "phone")
            added += 1

        for b in blocked:
            if not isinstance(b, dict) or b.get("thread") is None:
                continue
            for a in b.get("addrs") or [str(b.get("addr", ""))]:
                if str(a):
                    flag(b["thread"], str(a))
        # The flat list is the BlockedNumber table plus every blocked conversation's
        # addresses; the latter were just recorded under their thread id, so recording
        # them again keyed by address would only double the count in the log.
        covered = {store.normalize_addr(str(a)) for b in blocked if isinstance(b, dict)
                   for a in (b.get("addrs") or [])}
        for a in addrs:
            a = str(a)
            if store.normalize_addr(a) in covered:
                continue
            flag(thread_of.get(store.normalize_addr(a)), a)

        if complete:
            # A phone-sourced junk verdict the phone no longer reports was unblocked
            # there. Desktop/auto verdicts are not the phone's to release.
            for key, was in current.items():
                if key in reported or not was["junk"] or was.get("source") != "phone":
                    continue
                store.set_verdict(was.get("thread"), was.get("addr", ""), False, "phone")
                released += 1
        print(f"phone block list: {added} new verdict(s), {released} released, "
              f"{len(blocked)} blocked conversations, {len(addrs)} addrs", flush=True)
        return self._reply(200, {"ok": True, "recorded": added, "released": released,
                                 "received": len(addrs)})

    def _bulk(self):
        """Backfill ingest. One request per batch instead of one per message --
        ten thousand round trips would take the best part of an hour and give the
        phone ten thousand chances to be interrupted mid-run."""
        obj, err = self._json_body(cap=MAXBULK)
        if obj is None:
            return err
        msgs = obj.get("messages")
        if not isinstance(msgs, list):
            return self._reply(400, {"error": "messages must be a list"})

        now = int(time.time())
        recs = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            body = str(m.get("body", ""))
            rec = {"v": 1, "id": str(m.get("id") or ""), "dir": str(m.get("dir", "in")),
                   # Backfilled messages keep their ORIGINAL time in rx, not the moment
                   # they were imported -- otherwise every historic message would sort
                   # as if it arrived today and the archive would be worthless.
                   "ts": int(m.get("ts", now)), "rx": int(m.get("ts", now)),
                   "addr": str(m.get("addr") or "unknown"),
                   "kind": str(m.get("kind", "sms")), "sub": int(m.get("sub", -1)),
                   "code": extract(body), "backfilled": True}
            if m.get("thread") is not None:
                rec["thread"] = m["thread"]
            # Backfill carries part digests without bytes; dropping them here is what
            # made a re-run upload images and then keep no reference to them.
            if isinstance(m.get("parts"), list) and m["parts"]:
                rec["parts"] = m["parts"]
            if BODIES:
                rec["body"] = body
            if rec["id"]:
                recs.append(rec)

        res = store.add_messages(recs)
        # No notification and no code view update: these are historic, and popping a
        # desktop notification for a two-year-old 2FA code would be absurd.
        print(f"backfill batch: {res['stored']} stored, {res['duplicates']} dup"
              + (f", {res['enriched']} enriched" if res.get("enriched") else ""), flush=True)
        return self._reply(200, {"ok": True, **res, "total": len(store.messages())})

    def _ack(self):
        obj, err = self._json_body()
        if obj is None:
            return err
        ids = obj.get("ids") or []
        if not isinstance(ids, list):
            return self._reply(400, {"error": "ids must be a list"})
        res = store.ack(ids, obj.get("results") or {})
        if res["acked"]:
            print(f"acked {len(res['acked'])} command(s), "
                  f"{res['archive_removed']} archive row(s) removed", flush=True)
        return self._reply(200, res)

    # ------------------------------------------------------------------- GET

    def do_HEAD(self):
        """Existence probe for an attachment.

        The phone's backfill is resumable per conversation, so an interrupted run
        repeats a thread. Without a probe it would re-send every image in it; with
        one, resuming costs a round trip per part instead of the bytes.
        """
        path = urlparse(self.path).path.rstrip("/")
        if not self._auth():
            return self._reply(401)
        if path.startswith("/attachments/"):
            held = store.has_attachment(path.rsplit("/", 1)[-1])
            self.send_response(200 if held else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path.rstrip("/")
        if not self._auth():
            return self._reply(401, {"error": "unauthorized"})
        if path == "/latest":
            return self._reply(200, current_code() or {"error": "no code"})
        if path == "/code":
            since = int(q.get("since", ["0"])[0])
            return self._reply(200, store.code_since(since) or {"error": "no code"})
        if path == "/messages":
            return self._reply(200, {"messages": store.messages(
                since=int(q.get("since", ["0"])[0]),
                limit=int(q.get("limit", ["200"])[0]),
                addr=q.get("addr", [None])[0])})
        if path == "/commands":
            return self._reply(200, {"commands": store.pending()})
        if path.startswith("/attachments/"):
            sha = path.rsplit("/", 1)[-1]
            try:
                p = store.attachment_path(sha)
            except ValueError:
                return self._reply(400, {"error": "bad digest"})
            if not p.exists():
                return self._reply(404, {"error": "not held"})
            data = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/quarantine":
            return self._reply(200, {"verdicts": [
                {"key": list(k), **v} for k, v in store.verdicts().items()]})
        return self._reply(404, {"error": "not found"})

    def log_message(self, *a):
        pass   # the default logger would put message content in the journal


def main() -> int:
    store.DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    srv = http.server.ThreadingHTTPServer((BIND, PORT), H)
    print(f"listening on {BIND}:{PORT} "
          f"(archive={len(store.messages())} msgs, pending={len(store.pending())} cmds, "
          f"bodies={'on' if BODIES else 'off'})", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
