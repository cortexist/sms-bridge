# sms-bridge

Self-hosted bridge between an Android phone and your computer: forwards SMS to an
append-only archive, replies from the desk, and gives local agents a text channel to you.

Three parts, one archive:

- **bridge/** — a small Python HTTP server, standard library only. The phone posts every
  message to it; it keeps them in `~/.sms2fa/messages.jsonl`, hands back a queue of
  commands (send, block, pin, fetch an attachment, backfill history), and serves the
  archive to the desktop. It never interprets a message: a 2FA code is read by whoever
  needs it. Runs on a tailnet address only.
- **bridge/tui.py** — a terminal client (Textual) with inline images where the terminal
  can draw them: read, reply, quarantine junk, open pictures.
- **desktop/** — a Quickshell (QML) window with the same design tokens as the phone app:
  threads, messages grouped the phone's way, contact photos, thumbnails that open with
  the system handler, ctrl+c on a message, a pairing QR code for the phone. Runs on any
  machine that can reach the bridge.

The phone side is [SMS & Forward](https://github.com/cortexist/sms-and-forward), a fork of
QUIK. Pairing is a QR code shown by the desktop; from then on the phone forwards every
message it receives or sends and applies what the desktop queues. Nothing listens on the
phone, and nothing leaves your machines.

`DESIGN.md` is the contract between the three: record shapes, endpoints, commands, the
live link, the agents channel. `skills/sms-bridge/` is how an agent on the box uses it.

## Running the bridge

```
git clone https://github.com/cortexist/sms-bridge ~/Workspaces/sms-bridge
head -c 32 /dev/urandom | base64 > ~/.sms2fa/token && chmod 600 ~/.sms2fa/token
SMS2FA_BIND=<tailnet address> SMS2FA_PORT=8090 python3 -m bridge.server
```

As a user service (`~/.config/systemd/user/sms-bridge.service`):

```
[Unit]
Description=sms-bridge: receive forwarded SMS, hand back queued commands

[Service]
WorkingDirectory=%h/Workspaces/sms-bridge
ExecStart=/usr/bin/python3 -m bridge.server
# Bound to the tailnet address, so it is unreachable from the LAN. Do NOT bind
# 0.0.0.0: this endpoint hands out login codes.
Environment=SMS2FA_BIND=<tailnet address>
Environment=SMS2FA_PORT=8090
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Agent addresses are `<id>@agents` by default. Set `SMS_AGENTS_DOMAIN` (for example
`agents.example.net`, a domain you own) so no outside sender can ever collide with them;
the pairing code passes it to the phone and the desktop reads it from the bridge.

Every request carries `Authorization: Bearer <token>`. The token is the only credential;
the phone gets it from the pairing QR code, the desktop reads it from `~/.sms2fa/token`.

Names and photos come from a vdir of vCards (`~/.local/share/contacts`, kept by vdirsyncer
from your CardDAV server); agents are cards too, in a second collection. Both optional.

## The terminal client

```
python3 -m venv .venv && .venv/bin/pip install textual textual-image rich pillow
.venv/bin/python -m bridge.tui
```

## The desktop

See `desktop/README.md`. In short: `pacman -S quickshell qrencode`, copy the token, run
`desktop/run.sh`.

## License

GPL-3.0, the same as the phone app it pairs with.
