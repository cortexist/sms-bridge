# sms-bridge — phone ⇄ box SMS bridge

Two halves. The phone forwards **every** message to this box; the box stores them, derives
2FA codes, and (later) sends. Agents running here close their own loop locally.

## Why forward everything rather than filter on the phone

The alternative — preset the numbers 2FA arrives from and forward only those — creates a
coordination problem that has no good solution: the phone would have to know which agent is
waiting for what, the list needs maintaining as services are added, and **an unknown sender is
silently missed**.

Forwarding unconditionally deletes the problem instead of solving it. The phone becomes
stateless (receive → POST → done). The agent's "I am waiting for a code" state never leaves
this box, where the agent already is:

```
agent: t0 = now()  →  submit login  →  wait_code(since=t0, timeout=120s)
```

That is a local file poll. Nothing crosses the network, so nothing can desynchronise, and a
code from a number nobody predicted still arrives.

## Storage — append-only JSONL, fsync per record

`~/.sms2fa/messages.jsonl`, mode 0600 in a 0700 directory.

Append-only is chosen **because this box silent-resets every few hours** (see
`~/co-tune/SILENT-RESET.md`). A hard reset mid-write truncates at most the trailing line, which
a reader skips; there is no index to corrupt and no recovery step. SQLite would give nicer
queries at the cost of a failure mode this machine actually exercises. Revisit if the TUI needs
real queries — but revisit knowing the trade.

### Record schema (v1)

| field | type | notes |
|---|---|---|
| `v` | int | schema version, `1` |
| `id` | string | **stable, phone-generated.** The dedup key — retries must reuse it |
| `dir` | `"in"` \| `"out"` | |
| `ts` | int | epoch seconds, phone's message timestamp |
| `rx` | int | epoch seconds, box receive time |
| `addr` | string | peer address (sender for `in`, recipient for `out`) |
| `body` | string | may be absent if body storage is disabled |
| `kind` | `"sms"` \| `"mms"` | |
| `sub` | int | subscription id, `-1` unknown (dual SIM) |
| `code` | string \| null | derived at ingest, not sent by the phone |

`id` is what makes the retry queue safe. The phone will retry across box resets and its own
reboots; without a stable id every retry duplicates.

## API

| method | path | purpose |
|---|---|---|
| `POST` | `/sms` | ingest. Accepts the v1 shape **and** the original `{from, body}` shape |
| `GET` | `/latest` | newest code within TTL — unchanged, existing callers keep working |
| `GET` | `/code?since=<epoch>` | **the loop-closer.** Newest code received after `since` |
| `GET` | `/messages?since=&limit=&addr=` | for the TUI |
| `GET` | `/location` | newest phone location (the `result` of the last acked `location` command) |
| `GET` | `/commands?wait=N` | the queue, holding up to N s (≤30) until something is queued — the phone's live link |
| `GET` | `/live` | the live hint: `{wanted, lan, bssids, ttl}` |
| `GET` | `/instructions?since=&limit=` | what the human typed into the AGENTS thread (outbound to AGENTS) |
| `POST` | `/presence` | a desktop app is open (touches `~/.sms2fa/presence`; the TUI does this directly) |

CLI: `--show` (unchanged), `--wait-code --since <epoch|now> --timeout <s>` — blocks until a
code arrives, exits 0 with the code on stdout, exit 1 on timeout. That is the agent-facing call.

## Security posture — this changes, and it changes for the worse

The original receiver held **one code for 600 s**. This holds **every SMS you ever receive**,
indefinitely, including every 2FA code. That is a materially different asset and the existing
mitigations do not stretch to cover it.

What carries over: tailnet-only bind, bearer token with constant-time compare, 0600/0700,
never logging bodies or codes. The code TTL still applies to the *code view* — `/latest` and
`/code` will not return anything stale — but the archive itself has no TTL, because a message
manager without history is not a message manager.

**What does not carry over: `/` is plain ext4 with no LUKS.** An imaged or stolen disk yields
the entire archive in cleartext. The knobs provided:

- `SMSBRIDGE_RETAIN_DAYS` — prune records older than N days at startup (`0` = keep forever)
- `SMSBRIDGE_STORE_BODIES=0` — store metadata and the derived code only, no message text.
  Kills the TUI's usefulness, so it is the wrong default here, but it is the right setting if
  the archive is ever judged too hot to hold.

Full-disk encryption is the real answer and is out of scope for this file. Decide it before the
archive has months of history in it, not after.

## Phone side

`SmsReceivedReceiver` already enqueues an expedited `ReceiveSmsWorker` via WorkManager. The
forwarder mirrors that: a second worker, `ForwardMessageWorker`, with a network constraint and
exponential backoff. WorkManager persists its queue across app death **and phone reboots**,
which is what makes "the box was down" a non-event rather than a lost message.

**`android.permission.INTERNET` is currently commented out** in
`presentation/src/main/AndroidManifest.xml`. QUIK deliberately has no network access. Enabling
it is a real change to the app's privacy posture and should be a conscious decision, not a
side effect — it is why the forwarder is written to be a no-op unless explicitly configured.

## Send path — later, but decide the host now

Box → phone → SMS needs the phone to accept inbound requests. **Put that listener inside QUIK,
not Termux.** QUIK is the default SMS app, already holds `SEND_SMS`, and starts on boot;
Termux has no Termux:Boot installed, so its sshd stays down after a phone reboot until the app
is opened by hand. The app is the reliable host for this channel; the terminal is not.

## Agent → human: `notify` and `location`

Two commands in the queue exist for agents that need to reach the human, not for the TUI.

**`notify {body}`** — the phone inserts `body` into its own inbox as a message from `AGENTS`
(`store.AGENT_ADDR`) and posts its ordinary new-message notification. No carrier is involved:
it is a content-provider insert, which is what a car console (MAP), a watch and Android Auto
read from. Contract for the phone side:

- the inserted message is **not forwarded back** here (it did not arrive by radio);
- `AGENTS` is alphanumeric and not dialable, so a reply typed into that thread is **never
  handed to the radio**; the phone POSTs it to `/sms` as `dir: "out", addr: "AGENTS"` — that is
  the human→agent channel, and the receiver never extracts a code from that address;
- ack `result`: `{"message": <phone message id>}`.

**`location`** — the phone answers in the ack `result`:
`{"wifi_ips": [...], "permission": "not-requested", "ts"}` plus the legacy fields
`lat, lon, acc_m, provider, wifi_ssid, wifi_bssid` as `null`. Since app 4.4.3 the phone asks
for no location permission (Play reviews background location as a feature in its own right),
so the only signal is the phone's own wifi address: inside one of the box's subnets
(`store.box_lan_networks()`) is a zero-error "at home"; anything else is "unknown", which
sends by default. App builds up to 4.4.2 still report the access point and a GPS fix, and the
classifier still honours them.

The gating lives on the box, in `agents/utils/location.py` + `notify.py`, with the perimeter
config in `~/.agents/perimeter.json` (`enabled: false` turns the gate off and every notify goes
through). Decision: same wifi as the box → home; else distance from the static home coordinate
minus the fix's accuracy radius beyond the perimeter → away; a fix that straddles the perimeter
or is too coarse → unknown, which sends by default (`when_unknown`), on the view that a spare
text is cheaper than a missed one.

## The live link (human replying from the desk)

Polling every fifteen minutes is fine for everything except a human who typed a reply on the
computer and is now looking at the phone. That human is next to the phone, so the phone is on
the same access point as the box — and that is the gate. Every `/commands` and `/sms` response
carries a `live` hint: `wanted` (a desktop app touched `~/.sms2fa/presence` within `ttl`
seconds), `lan` (the box's IPv4 subnets plus whatever subnets the open desktop reported with
its presence, so a desktop on another machine counts as "here" too) and `bssids` (kept for
app builds up to 4.4.2). When
`wanted` is true and the phone's own wifi address lies in `lan`, the phone opens a foreground
service that long-polls
`/commands?wait=25`; a queued `send` then leaves within a second. When either condition ends
the service stops and the queue is back to polling. The phone still initiates every connection
and nothing listens on it; the only cost is a silent notification while the link is up.

## Instructions (human → agent)

A reply typed into the AGENTS thread on the phone is a real sent SMS in the phone's log that the
radio never saw; the phone forwards it as `dir: "out", addr: "AGENTS"`. `/instructions` (or the
archive file) lists them. As with 2FA codes, the bridge does not interpret them: an agent takes a
watermark, waits for what arrives, and answers through `notify`, which lands in the same thread.
