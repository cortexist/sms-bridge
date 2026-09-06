import QtQuick

// One conversation in the list: avatar, name, preview, time. Pinned first is the bridge's
// ordering; the row only draws. Selected state is a low-alpha tint, Omarchy style.
Rectangle {
    id: root
    property var thread
    property bool current: false
    signal clicked()
    // Three lines of text, as a terminal would lay it out: half a line of air, the name,
    // the preview, half a line of air -- so a row's highlight covers exactly its three lines
    // and the first row has its air above it.
    readonly property real line: Theme.lineHeight
    implicitHeight: Math.round(3 * line)
    color: current ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.18) : "transparent"

    // No rule between rows: the half line of air above and below each row separates them.

    Avatar {
        id: avatar
        thread: root.thread
        failed: !!(root.thread && root.thread.last_failed)
        anchors.left: parent.left; anchors.leftMargin: Theme.pad
        y: Math.round(1.5 * root.line - height / 2)    // centred on the two text lines
    }
    Column {
        anchors.left: avatar.right; anchors.leftMargin: Theme.pad
        anchors.right: stamp.left; anchors.rightMargin: Theme.pad
        y: Math.round(0.5 * root.line)
        spacing: 0
        Row {
            id: titleRow
            spacing: 6; height: root.line; width: parent.width
            Text {
                text: Bridge.title(root.thread)
                color: Theme.brightForeground; font.family: Theme.font; font.pixelSize: Theme.fontSize; font.bold: true
                elide: Text.ElideRight
                // Elide against what is left after the marks, never under the timestamp.
                width: Math.max(0, Math.min(implicitWidth, titleRow.width - marks.width - (marks.width ? titleRow.spacing : 0)))
                height: root.line; verticalAlignment: Text.AlignVCenter
            }
            Row {
                id: marks
                spacing: 6; height: root.line
                Text { visible: !!(root.thread && root.thread.pinned); text: "▲"; color: Theme.darkForeground; font.family: Theme.font; font.pixelSize: Theme.fontSmall; height: root.line; verticalAlignment: Text.AlignVCenter }
                Text {   // quarantined, and by whom: the phone's block or a verdict on the box
                    visible: !!(root.thread && root.thread.junk)
                    text: root.thread && root.thread.junk_source === "phone" ? "blocked on phone" : "junk"
                    color: Theme.yellow; font.family: Theme.font; font.pixelSize: Theme.fontSmall
                    height: root.line; verticalAlignment: Text.AlignVCenter
                }
            }
        }
        Text {
            width: parent.width
            text: root.thread ? ((root.thread.last_out ? "you: " : "") + (root.thread.preview || "").replace(/\s+/g, " ")) : ""
            // Unread, as on the phone: the preview in bold and the bright text colour, with a
            // small oval in the conversation's colour at the end of the line.
            readonly property bool unread: !!(root.thread && root.thread.unread)
            color: root.thread && root.thread.last_failed ? Theme.red : (unread ? Theme.brightForeground : Theme.foreground)
            font.family: Theme.font; font.pixelSize: Theme.fontSmall; font.bold: unread
            elide: Text.ElideRight; maximumLineCount: 1
            height: root.line; verticalAlignment: Text.AlignVCenter
            rightPadding: unread ? 24 : 0
        }
    }
    Text {
        id: stamp
        anchors.right: parent.right; anchors.rightMargin: Theme.pad
        y: Math.round(0.5 * root.line)
        height: root.line; verticalAlignment: Text.AlignVCenter   // on the first line, with the name
        text: root.thread ? Bridge.when(root.thread.last) : ""
        color: Theme.darkForeground; font.family: Theme.font; font.pixelSize: Theme.fontSmall
        font.bold: !!(root.thread && root.thread.unread)
    }
    Rectangle {   // unread: a filled dot on the second line, in the conversation's colour
        visible: !!(root.thread && root.thread.unread)
        width: 10; height: width; radius: width / 2
        color: Bridge.tint(root.thread)
        anchors.right: parent.right; anchors.rightMargin: Theme.pad + 5
        y: Math.round(1.5 * root.line + (root.line - height) / 2)
    }
    MouseArea { anchors.fill: parent; onClicked: root.clicked() }
}
