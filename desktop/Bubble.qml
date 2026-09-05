import QtQuick
import QtQuick.Effects

// One message, laid out by the phone's rules (MessagesAdapter + BubbleUtils) so the same
// thread reads the same way on every device:
//  - a timestamp line above a message when ten minutes or more passed since the previous;
//  - adjacent messages from the same sender under ten minutes apart form a group: no gap
//    between them, and with QUIK bubbles the corners facing a neighbour are flattened;
//  - each picture or video is its own borderless block, a 280 px centre-cropped square
//    with the bubble's corners, then the text (and other parts, by name) in a bubble;
//  - incoming blocks in the conversation's colour (the avatar's), outgoing in the accent
//    or grey per the option; no time inside; a failed send says so underneath;
//  - the sender's avatar beside the last block of an incoming run.
Item {
    id: root
    property var message
    property var previous: null
    property var next: null
    property var thread: null
    property int maxWidth: 420
    readonly property int avatarSize: 28
    readonly property bool out: !!(message && message.dir === "out")
    readonly property bool failed: out && message && message.status === "failed"
    readonly property int thresholdMin: 10

    function stamp(m) { return m ? (m.ts || m.rx || 0) : 0 }
    function sameSender(a, b) { return !!a && !!b && a.dir === b.dir && (a.dir === "out" || a.addr === b.addr) }
    function canGroup(a, b) { return sameSender(a, b) && Math.abs(stamp(a) - stamp(b)) < thresholdMin * 60 }
    readonly property bool groupedWithPrevious: canGroup(message, previous)
    readonly property bool groupedWithNext: canGroup(message, next)
    readonly property bool showTimestamp: !previous || (stamp(message) - stamp(previous)) >= thresholdMin * 60
    readonly property bool selectedMsg: Bridge.sameMessage(root.message, Bridge.selectedMessage)

    function isMedia(p) { return !!(p.sha && (/^image\//.test(p.mime || "") || /^video\//.test(p.mime || ""))) }
    readonly property var parts: (message && message.parts) ? message.parts : []
    readonly property var media: parts.filter(isMedia)
    readonly property var others: parts.filter(p => !isMedia(p))
    readonly property bool hasText: !!(message && message.body && message.body.length)
    readonly property bool hasTextBlock: hasText || others.length > 0
    readonly property int blocks: media.length + (hasTextBlock ? 1 : 0)
    readonly property real indent: out ? 0 : avatarSize + Theme.pad
    readonly property real inner: maxWidth - indent
    readonly property bool tinted: failed || !out || Theme.outgoingAccent
    readonly property color blockColor: failed ? Theme.red : !out ? Bridge.tint(thread) : Theme.outgoingAccent ? Theme.accent : Theme.lighterBackground
    readonly property real r: Theme.bubbleStyle === 1 ? 18 : 0
    readonly property real flat: Theme.bubbleStyle === 1 ? 4 : 0
    // Corners of block i of this message: flattened toward a neighbour block, or toward a
    // grouped neighbour message at the ends of the run.
    function topFlat(i) { return i > 0 || groupedWithPrevious }
    function bottomFlat(i) { return i < blocks - 1 || groupedWithNext }

    // The phone's DateFormatter: time today, weekday and time inside a week, date beyond.
    function headerText(ts) {
        const d = new Date(ts * 1000), now = new Date()
        const t = d.toLocaleTimeString(Qt.locale(), "HH:mm")
        if (d.toDateString() === now.toDateString()) return t
        if (now - d < 6 * 86400000) return d.toLocaleDateString(Qt.locale(), "ddd") + " " + t
        if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString(Qt.locale(), "MMM d") + " " + t
        return d.toLocaleDateString(Qt.locale(), "MMM d yyyy") + " " + t
    }

    implicitHeight: column.height + (groupedWithNext ? 2 : 16)

    // The avatar beside the last block of an incoming run.
    component SideAvatar: Avatar {
        required property bool last
        visible: !root.out && !root.groupedWithNext && last
        thread: root.thread
        width: root.avatarSize; height: root.avatarSize
        anchors.left: parent.left; anchors.bottom: parent.bottom
    }

    Column {
        id: column
        width: parent.width
        spacing: 0

        Text {   // the timestamp line, centred, like the phone's
            visible: root.showTimestamp
            width: parent.width; horizontalAlignment: Text.AlignHCenter
            height: visible ? Theme.lineHeight * 1.6 : 0; verticalAlignment: Text.AlignVCenter
            text: root.showTimestamp ? root.headerText(root.stamp(root.message)) : ""
            color: Theme.darkForeground; font.family: Theme.font; font.pixelSize: Theme.fontSmall
        }

        // Pictures and videos: borderless squares, edge to edge, click to open the original
        // with the system's handler for the type.
        Repeater {
            model: root.media
            Item {
                id: mediaBlock
                required property var modelData
                required property int index
                readonly property bool video: /^video\//.test(modelData.mime || "")
                readonly property string src: Bridge.attachmentSource(modelData.thumb || (video ? "" : modelData.sha), root.message ? root.message.id : null)
                readonly property real side: Math.min(root.inner, 280)
                width: parent.width; height: side + (index < root.blocks - 1 ? 2 : 0)
                SideAvatar { last: index === root.blocks - 1 }
                Item {
                    id: square
                    width: mediaBlock.side; height: mediaBlock.side
                    anchors.right: root.out ? parent.right : undefined
                    anchors.left: root.out ? undefined : parent.left
                    anchors.leftMargin: root.indent
                    Rectangle {   // placeholder while the thumbnail is on its way (or the phone is being asked)
                        anchors.fill: parent; visible: thumb.status !== Image.Ready
                        color: root.blockColor
                        topLeftRadius: root.topFlat(mediaBlock.index) ? root.flat : root.r
                        topRightRadius: root.topFlat(mediaBlock.index) ? root.flat : root.r
                        bottomLeftRadius: root.bottomFlat(mediaBlock.index) ? root.flat : root.r
                        bottomRightRadius: root.bottomFlat(mediaBlock.index) ? root.flat : root.r
                        Text {
                            anchors.centerIn: parent
                            text: mediaBlock.src ? "…" : (mediaBlock.video ? "▶ video" : "▣ " + (mediaBlock.modelData.name || "image"))
                            color: Theme.textOn(root.blockColor); font.family: Theme.font; font.pixelSize: Theme.fontSmall
                        }
                    }
                    Image {
                        id: thumb
                        anchors.fill: parent
                        source: mediaBlock.src
                        fillMode: Image.PreserveAspectCrop; asynchronous: true
                        sourceSize: Qt.size(560, 560)
                        visible: false; layer.enabled: status === Image.Ready
                    }
                    Item {
                        id: thumbMask
                        anchors.fill: parent; visible: false; layer.enabled: thumb.status === Image.Ready
                        Rectangle {
                            anchors.fill: parent; color: "#000000"
                            topLeftRadius: root.topFlat(mediaBlock.index) ? root.flat : root.r
                            topRightRadius: root.topFlat(mediaBlock.index) ? root.flat : root.r
                            bottomLeftRadius: root.bottomFlat(mediaBlock.index) ? root.flat : root.r
                            bottomRightRadius: root.bottomFlat(mediaBlock.index) ? root.flat : root.r
                        }
                    }
                    MultiEffect {
                        anchors.fill: parent; visible: thumb.status === Image.Ready
                        source: thumb; maskEnabled: true; maskSource: thumbMask
                        maskThresholdMin: 0.5; maskSpreadAtMin: 1.0
                    }
                    Rectangle {   // play mark over a video frame
                        visible: mediaBlock.video && thumb.status === Image.Ready
                        anchors.centerIn: parent; width: 36; height: 36; radius: 18
                        color: Qt.rgba(0, 0, 0, 0.55)
                        Text { anchors.centerIn: parent; anchors.horizontalCenterOffset: 2; text: "▶"; color: "#ffffff"; font.pixelSize: 16 }
                    }
                    MouseArea {
                        anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                        onClicked: Bridge.openAttachment(mediaBlock.modelData, root.message ? root.message.id : null)
                    }
                }
            }
        }

        // The text, then any part that is not a picture or video, by name.
        Item {
            visible: root.hasTextBlock
            width: parent.width; height: visible ? bubble.height : 0
            SideAvatar { last: true }
            Rectangle {
                id: bubble
                readonly property int i: root.blocks - 1
                anchors.right: root.out ? parent.right : undefined
                anchors.left: root.out ? undefined : parent.left
                anchors.leftMargin: root.indent
                width: Math.min(body.implicitWidth, root.inner) + 2 * Theme.pad
                height: body.implicitHeight + 2 * Theme.pad
                topLeftRadius: root.topFlat(i) ? root.flat : root.r
                topRightRadius: root.topFlat(i) ? root.flat : root.r
                bottomLeftRadius: root.bottomFlat(i) ? root.flat : root.r
                bottomRightRadius: root.bottomFlat(i) ? root.flat : root.r
                color: root.blockColor
                // Selected: an outline in the foreground colour, readable on any tint.
                border.width: root.selectedMsg ? 2 : (Theme.bubbleStyle === 1 || root.tinted ? 0 : Theme.border)
                border.color: root.selectedMsg ? Theme.brightForeground : Theme.muted
                MouseArea {   // click selects the message; ctrl+c then copies its text
                    anchors.fill: parent
                    onClicked: Bridge.selectedMessage = root.selectedMsg ? null : root.message
                }
                Text {
                    id: body
                    x: Theme.pad; y: Theme.pad
                    width: Math.min(implicitWidth, root.inner)
                    text: (root.hasText ? root.message.body : "")
                        + root.others.map(p => (root.hasText ? "\n" : "") + "▣ " + (p.name || p.mime || "attachment")
                            + (p.skipped ? " (" + p.skipped + ")" : "")).join("")
                    font.italic: !root.hasText
                    wrapMode: Text.Wrap
                    color: root.tinted ? Theme.textOn(root.blockColor) : Theme.brightForeground
                    font.family: Theme.font; font.pixelSize: Theme.fontSize
                }
            }
        }

        Text {   // status line under the bubble, as on the phone: only when there is something to say
            visible: root.failed
            width: parent.width; horizontalAlignment: root.out ? Text.AlignRight : Text.AlignLeft
            leftPadding: root.indent
            height: visible ? Theme.lineHeight : 0; verticalAlignment: Text.AlignVCenter
            text: "failed"
            color: Theme.red; font.family: Theme.font; font.pixelSize: Theme.fontSmall
        }
    }
}
