# SMS desktop

The bridge's archive as a two-pane messaging window, in Quickshell. A client of the
sms-bridge on the box; it holds no data of its own beyond the options file. Runs on any
machine on the tailnet, the Omarchy laptop included: there is one bridge, and this app
only talks to it.

## Setup on a machine

1. `sudo pacman -S quickshell qrencode` (qrencode only renders the pairing code).
2. Copy the bridge token from the box: `scp <box>:~/.sms2fa/token ~/.sms2fa/token`
   and `chmod 600` it. That is the only credential the app needs.
3. From a clone of sms-bridge, run `desktop/run.sh`. The bridge address defaults to
   `http://100.64.0.3:8090`; set `SMS_BRIDGE_URL` to override.

Options (control shape, boxes or bubbles, outgoing colour, theme) live in
`~/.sms-desktop/settings.json`, per machine, and are edited from the `options` button.

## Things the phone cannot do

- Click a message, then ctrl+c: its text goes to the system clipboard (via wl-copy). The
  reply field's own selection wins when it has one.
- Click a picture or video: the original opens with the system's handler for the type,
  fetched from the bridge (or from the phone first, if the bridge only has the digest).

## What works from another machine

- Everything in the window: threads, messages, replies, quarantine, contact photos
  (served by the bridge), pairing (the QR carries the bridge address and token).
- The live link: presence carries this machine's own subnets, so the phone opens the
  fast path when it is on the same network as the open desktop, at home or away.
- Not here: system notifications. Those are the bridge's job and appear on the box.
