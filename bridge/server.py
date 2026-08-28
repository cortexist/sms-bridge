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
from bridge import store  # noqa: E402

TOKEN = (store.DIR / "token").read_text().strip()
BIND = os.environ.get("SMS2FA_BIND", "100.99.132.67")
PORT = int(os.environ.get("SMS2FA_PORT", "8090"))
TTL = int(os.environ.get("SMS2FA_TTL", "600"))
BODIES = os.environ.get("SMSBRIDGE_STORE_BODIES", "1") != "0"
# 256 KiB is right for a single forwarded message; a backfill batch is a legitimate
# large payload, so the cap is per-endpoint rather than global.
MAXBODY = 262144
MAXBULK = 8 * 1024 * 1024

# Prefer a digit run sitting near a word suggesting it is the code; fall back to any
# plausible standalone run. Ordered most- to least-specific.
PATTERNS = [
    re.compile(r"(?:code|otp|pin|passcode|verification|verify|2fa)\D{0,20}(\d{4,8})", re.I),
    re.compile(r"(\d{4,8})\D{0,20}(?:is your|as your)", re.I),
    re.compile(r"\b(\d{6})\b"),
    re.compile(r"\b(\d{4,8})\b"),
]


def extract(body: str):
    for p in PATTERNS:
        m = p.search(body or "")
        if m:
            return m.group(1)
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

        # Piggyback: the response carries whatever the desktop queued.
        return self._reply(200, {"ok": True, "commands": store.pending()})

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
            if BODIES:
                rec["body"] = body
            if rec["id"]:
                recs.append(rec)

        res = store.add_messages(recs)
        # No notification and no code view update: these are historic, and popping a
        # desktop notification for a two-year-old 2FA code would be absurd.
        print(f"backfill batch: {res['stored']} stored, {res['duplicates']} dup", flush=True)
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
