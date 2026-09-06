pragma Singleton
import QtQuick
import Quickshell
import Quickshell.Io

// The bridge client. Same machine as the bridge, so the token is read straight from
// ~/.sms2fa/token; everything else is the bridge's HTTP API over the tailnet address.
// Polling is deliberate: the archive is an append-only file the bridge owns, and a
// few small GETs a second on loopback-speed links cost nothing.
QtObject {
    id: root

    property string base: Quickshell.env("SMS_BRIDGE_URL") || "http://100.64.0.3:8090"
    property string token: ""
    property bool ready: token.length > 0

    property var allThreads: []          // every thread from /threads, verdicts included
    property var threads: []             // what the list shows: quarantined or not, per showJunk
    property bool showJunk: false        // the quarantine view, like the TUI's Q
    readonly property int junkCount: allThreads.filter(t => !!t.junk).length
    onShowJunkChanged: applyThreadFilter()

    function applyThreadFilter() {
        threads = allThreads.filter(t => !!t.junk === showJunk)
        if (selected && !threads.find(t => sameThread(t, selected)))
            selected = threads.length ? threads[0] : null   // a thread just blocked leaves the pane
        if (selected) refreshMessages()
    }
    property var selected: null          // a thread row from /threads
    property var messages: []
    property var live: ({ wanted: false, link: null })
    property string error: ""

    // ---------------------------------------------------------------- pairing
    // The phone pairs by scanning a QR code: smsforward://pair?endpoint=<url>&token=<token>,
    // endpoint being the full URL the phone posts to. qrencode renders it to a PNG in the
    // user's runtime directory (private, tmpfs, gone at logout); the file is removed when
    // the overlay closes so the token does not sit around as an image.
    readonly property string pairPayload: "smsforward://pair?endpoint=" + encodeURIComponent(base + "/sms") + "&token=" + encodeURIComponent(token)
        + "&agents=" + encodeURIComponent(agentDomain)

    // The domain of agent addresses, from the bridge (SMS_AGENTS_DOMAIN there), so every
    // side agrees on what is an agent thread.
    property string agentDomain: "agents"
    function refreshAgents() { request("GET", "/agents", null, function(r) { if (r && r.domain) root.agentDomain = String(r.domain).toLowerCase() }) }
    property Timer agentsTimer: Timer { interval: 300000; running: root.ready; repeat: true; triggeredOnStart: true; onTriggered: root.refreshAgents() }
    readonly property string qrPath: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/sms-desktop-pair.png"
    property int qrVersion: 0            // bumps so the Image reloads a regenerated file
    property bool qrReady: false
    property string qrError: ""

    property Process qrProc: Process {
        command: ["qrencode", "-o", root.qrPath, "-s", "6", "-m", "2", "-l", "M", root.pairPayload]
        onExited: (code, status) => {
            if (code === 0) { root.qrVersion++; root.qrReady = true; root.qrError = "" }
            else { root.qrReady = false; root.qrError = "qrencode failed (" + code + "); pacman -S qrencode" }
        }
    }
    property Process qrRm: Process { command: ["rm", "-f", root.qrPath] }

    function makePairCode() { if (!ready) return; qrReady = false; qrProc.running = true }
    function dropPairCode() { qrReady = false; qrRm.running = true }

    property FileView tokenFile: FileView {
        path: Quickshell.env("HOME") + "/.sms2fa/token"
        blockLoading: true
        onLoaded: root.token = text().trim()
        onLoadFailed: root.error = "no token at ~/.sms2fa/token"
    }

    // `onFail(status)` for callers that expect a miss (a thumbnail the bridge does not
    // hold): it is called instead of the header error being set.
    function request(method, path, body, cb, onFail) {
        if (!ready) return
        const xhr = new XMLHttpRequest()
        xhr.open(method, base + path)
        xhr.setRequestHeader("Authorization", "Bearer " + token)
        if (body !== null) xhr.setRequestHeader("Content-Type", "application/json")
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE) return
            if (xhr.status === 200) {
                root.error = ""
                try { cb(JSON.parse(xhr.responseText)) } catch (e) { root.error = "bad reply: " + e }
            } else if (onFail) {
                onFail(xhr.status)
            } else {
                root.error = (xhr.status ? "http " + xhr.status : "bridge unreachable") + " on " + path
            }
        }
        xhr.send(body === null ? undefined : JSON.stringify(body))
    }

    function refreshThreads() {
        request("GET", "/threads", null, function(r) {
            root.allThreads = r.threads
            const shown = r.threads.filter(t => !!t.junk === root.showJunk)
            root.threads = shown
            const again = root.selected ? shown.find(t => sameThread(t, root.selected)) : null
            if (again) {
                root.selected = again
            } else if (shown.length) {
                select(shown[0])              // open on the newest conversation, never a blank pane
            } else {
                root.selected = null
            }
        })
    }

    function sameThread(a, b) {
        if (!a || !b) return false
        if (a.thread !== null && a.thread !== undefined && b.thread !== null && b.thread !== undefined) return a.thread === b.thread
        return a.norm === b.norm
    }

    function refreshMessages() {
        if (!selected) { root.messages = []; return }
        const t = selected
        const q = "?limit=200" + (t.thread !== null && t.thread !== undefined ? "&thread=" + t.thread : "")
                + (t.addrs && t.addrs.length ? "&addrs=" + encodeURIComponent(t.addrs.join(",")) : "")
        request("GET", "/messages" + q, null, function(r) {
            if (sameThread(t, root.selected)) root.messages = r.messages
        })
    }

    function refreshLive() { request("GET", "/live", null, function(r) { root.live = r }) }
    // Presence carries this machine's own IPv4 subnets, so a desktop on another machine
    // (the laptop, away from home) lets the phone open the live link when it is on the
    // same network as the open desktop. Read from `ip`, refreshed every minute.
    property var lan: []
    property Process lanProc: Process {
        command: ["ip", "-4", "-o", "addr", "show", "scope", "global"]
        stdout: StdioCollector {
            onStreamFinished: {
                const nets = []
                for (const line of text.split("\n")) {
                    const f = line.trim().split(/\s+/)
                    if (f.length < 4 || /^(tailscale|docker|veth|lo)/.test(f[1])) continue
                    const m = f[3].match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/)
                    if (!m) continue
                    const bits = parseInt(m[5]); if (bits >= 32) continue
                    const ip = ((+m[1] << 24) | (+m[2] << 16) | (+m[3] << 8) | (+m[4])) >>> 0
                    const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0
                    const n = (ip & mask) >>> 0
                    nets.push([n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join(".") + "/" + bits)
                }
                root.lan = nets
            }
        }
    }
    property Timer lanTimer: Timer { interval: 60000; running: true; repeat: true; triggeredOnStart: true; onTriggered: root.lanProc.running = true }
    function presence() { request("POST", "/presence", { lan: root.lan }, function(r) { root.live = r.live }) }

    // ---------------------------------------------------------------- attachments
    // Thumbnails come as base64 JSON (same reason as photos) and are cached by digest.
    // A digest the bridge does not hold is requested from the phone once, through the
    // command queue, and re-tried while the bubble is on screen.
    property var attachments: ({})
    property var attachmentsPending: ({})
    property var attachmentsMissing: ({})
    function attachmentSource(sha, messageId) {
        if (!sha) return ""
        if (attachments[sha]) return attachments[sha]
        if (!attachmentsPending[sha]) {
            attachmentsPending[sha] = true
            request("GET", "/attachments/" + sha + "?b64=1", null, function(r) {
                const next = Object.assign({}, root.attachments); next[sha] = "data:application/octet-stream;base64," + r.data
                root.attachments = next
                const m = Object.assign({}, root.attachmentsMissing); delete m[sha]; root.attachmentsMissing = m
            }, function(status) {
                if (!root.attachmentsMissing[sha] && messageId && status === 404)
                    request("POST", "/commands", { op: "fetch_attachment", args: { sha: sha, message: String(messageId) } }, function() {})
                const m = Object.assign({}, root.attachmentsMissing); m[sha] = Date.now(); root.attachmentsMissing = m
                delete root.attachmentsPending[sha]      // allow a retry
            })
        }
        return ""
    }
    property Timer attachmentRetry: Timer {
        interval: 4000; running: Object.keys(root.attachmentsMissing).length > 0; repeat: true
        onTriggered: {
            // Only digests asked for in the last two minutes: the phone answers within a
            // poll, or it will not (an image it no longer has).
            const now = Date.now()
            for (const sha in root.attachmentsMissing)
                if (now - root.attachmentsMissing[sha] < 120000 && !root.attachmentsPending[sha]) {
                    root.attachmentsPending[sha] = true
                    request("GET", "/attachments/" + sha + "?b64=1", null, function(r) {
                        delete root.attachmentsPending[sha]
                        const next = Object.assign({}, root.attachments); next[sha] = "data:application/octet-stream;base64," + r.data
                        root.attachments = next
                        const m = Object.assign({}, root.attachmentsMissing); delete m[sha]; root.attachmentsMissing = m
                        const job = root.openWhenHeld[sha]
                        if (job) {                       // the original a click was waiting for
                            delete root.openWhenHeld[sha]
                            if (root.error.indexOf("asking the phone") === 0) root.error = ""
                            root.openQueue = root.openQueue.concat([job]); root.runNextOpen()
                        }
                    }, function() { delete root.attachmentsPending[sha] })
                }
        }
    }

    // Open an attachment with the system's handler for its type: fetched to the runtime
    // directory under its digest and extension (works from any machine), then xdg-open.
    // The token goes through the environment, never the command line.
    readonly property string cacheDir: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/sms-desktop"
    property var openQueue: []
    property var openWhenHeld: ({})      // digest -> open job, for originals the phone still has to send
    property Process opener: Process {
        command: ["sh", "-c", 'mkdir -p "$OUT_DIR" && curl -sf -H "Authorization: Bearer $SMS_TOKEN" "$URL" -o "$OUT" && xdg-open "$OUT"']
        onExited: (code) => { if (code !== 0) root.error = "could not open attachment (" + code + ")"; root.runNextOpen() }
    }
    function extFor(part) {
        const name = (part.name || ""), dot = name.lastIndexOf(".")
        if (dot > 0 && name.length - dot <= 5) return name.slice(dot + 1).toLowerCase()
        const m = (part.mime || "").split("/")[1] || "bin"
        return { "jpeg": "jpg", "quicktime": "mov", "3gpp": "3gp" }[m] || m
    }
    function openAttachment(part, messageId) {
        if (!part || !part.sha) return
        openQueue = openQueue.concat([{ sha: part.sha, ext: extFor(part), message: messageId }])
        if (!opener.running) runNextOpen()
    }
    function runNextOpen() {
        if (!openQueue.length || opener.running) return
        const job = openQueue[0]; openQueue = openQueue.slice(1)
        // Make sure the bridge holds it (ask the phone if not), then hand it to the opener.
        request("GET", "/attachments/" + job.sha + "?b64=1", null, function(r) {
            root.error = ""
            opener.environment = ({ SMS_TOKEN: root.token, URL: root.base + "/attachments/" + job.sha,
                                    OUT_DIR: root.cacheDir, OUT: root.cacheDir + "/" + job.sha.slice(0, 16) + "." + job.ext })
            opener.running = true
        }, function() {
            // Not held yet (a backfilled record carries digests only): ask the phone, and
            // open as soon as the retry loop sees it arrive.
            openWhenHeld[job.sha] = job
            attachmentSource(job.sha, job.message)
            root.error = "asking the phone for the original…"
            runNextOpen()
        })
    }

    // Contact photos come through the API as base64 (an Image cannot send the bearer
    // header), cached here by URL path as data URLs. Reassigned as a whole so bindings
    // on photos[...] re-evaluate.
    property var photos: ({})
    property var photosPending: ({})
    function photoSource(t) {
        const p = t && t.photo
        if (!p) return ""
        if (photos[p]) return photos[p]
        if (!photosPending[p]) {
            photosPending[p] = true
            request("GET", p, null, function(r) {
                if (!r || !r.data) return
                const next = Object.assign({}, root.photos); next[p] = "data:" + r.type + ";base64," + r.data
                root.photos = next
            })
        }
        return ""
    }

    function send(addr, body, cb) {
        request("POST", "/commands", { op: "send", args: { addr: addr, body: body } }, function(r) { if (cb) cb(r) })
    }

    function select(t) { selected = t; messages = []; selectedMessage = null; refreshMessages() }

    // A selected message: click a bubble, ctrl+c copies its text to the system clipboard.
    // Message-level text is the unit; the phone has no equivalent, the desktop should.
    property var selectedMessage: null
    property string notice: ""
    property Timer noticeTimer: Timer { interval: 2500; onTriggered: root.notice = "" }
    function sameMessage(a, b) { return !!a && !!b && a.id === b.id && (a.ts || a.rx) === (b.ts || b.rx) }
    function copySelected() {
        const m = selectedMessage
        if (!m) return false
        const text = (m.body && m.body.length) ? m.body : ""
        if (!text.length) { notice = "nothing to copy: no text in that message"; noticeTimer.restart(); return true }
        // wl-copy rather than Qt's clipboard: the latter only takes while this window
        // is focused. Text via the environment, never the command line.
        clipProc.environment = ({ TEXT: text })
        clipProc.running = true
        Quickshell.clipboardText = text
        notice = "copied"; noticeTimer.restart()
        return true
    }
    property Process clipProc: Process {
        command: ["sh", "-c", 'printf %s "$TEXT" | wl-copy']
        onExited: (code) => { if (code !== 0) root.notice = "copy failed: is wl-copy installed?" }
    }

    property Timer threadTimer: Timer { interval: 3000; running: root.ready; repeat: true; triggeredOnStart: true; onTriggered: root.refreshThreads() }
    property Timer messageTimer: Timer { interval: 2000; running: root.ready; repeat: true; onTriggered: root.refreshMessages() }
    property Timer liveTimer: Timer { interval: 5000; running: root.ready; repeat: true; triggeredOnStart: true; onTriggered: root.refreshLive() }
    // "A human is at the desk": what the phone's live link keys on.
    property Timer presenceTimer: Timer { interval: 20000; running: root.ready; repeat: true; triggeredOnStart: true; onTriggered: root.presence() }

    // Helpers shared by the views
    function pretty(a) {
        if (!a) return "?"
        const d = a.replace(/\D/g, "")
        if (d.length === 11 && d[0] === "1") return "+1 (" + d.slice(1, 4) + ") " + d.slice(4, 7) + "-" + d.slice(7)
        if (d.length === 10) return "(" + d.slice(0, 3) + ") " + d.slice(3, 6) + "-" + d.slice(6)
        return a
    }
    function when(ts) {
        if (!ts) return ""
        const d = new Date(ts * 1000), now = new Date()
        const sameDay = d.toDateString() === now.toDateString()
        if (sameDay) return d.toLocaleTimeString(Qt.locale(), "HH:mm")
        if (now - d < 6 * 86400000) return d.toLocaleDateString(Qt.locale(), "ddd")
        return d.toLocaleDateString(Qt.locale(), "MMM d")
    }
    // No contact names reach the bridge (the phone forwards messages, not contacts), so the
    // avatar carries the last two digits of the number, the part people actually remember,
    // and a colour hashed from the address -- the same "auto colour" idea as the phone.
    function isAgent(t) { return !!t && (t.addr === "AGENTS" || (t.addr || "").toLowerCase().endsWith("@" + agentDomain)) }
    function title(t) {
        if (!t) return ""
        if (t.agent) return t.agent.name || t.name || t.addr
        if (t.addr === "AGENTS") return t.name || "Chief"     // Chief's old address
        return t.name || pretty(t.addr)
    }
    function initials(t) {
        if (!t) return "?"
        if (isAgent(t)) return "``"
        if (t.name) {
            const w = t.name.trim().split(/\s+/)
            return (w.length > 1 ? w[0][0] + w[w.length - 1][0] : w[0].slice(0, 2)).toUpperCase()
        }
        const d = (t.addr || "").replace(/\D/g, "")
        if (d.length >= 2) return d.slice(-2)
        return (t.addr || "?").slice(0, 2).toUpperCase()
    }
    readonly property var palette: ["#7aa2f7", "#bb9af7", "#7dcfff", "#9ece6a", "#e0af68", "#ff9e64", "#f7768e", "#0db9d7", "#b9f27c", "#ad8ee6"]
    function tint(t) {
        if (t && t.agent && t.agent.color) return t.agent.color
        if (!t || isAgent(t)) return Theme.accent
        let h = 0; const a = t.norm || t.addr || ""
        for (let i = 0; i < a.length; i++) h = (h * 31 + a.charCodeAt(i)) >>> 0
        return palette[h % palette.length]
    }
}
