import QtQuick
import QtQuick.Controls.Basic
import Quickshell

// SMS desktop: the bridge's archive as a two-pane messaging window. A plain Quickshell
// window here; on Omarchy the same components become a panel plugin.
ShellRoot {
    FloatingWindow {
        id: win
        title: "sms"
        implicitWidth: 1040; implicitHeight: 680
        minimumSize: Qt.size(720, 420)
        color: Theme.background
        // Quickshell is a shell: closing its window (sway's kill, the close button) only
        // hides it and the process stays. This is an app with one window, so closing it
        // ends the process.
        onVisibleChanged: if (!visible) Qt.quit()

        // ctrl+c: the reply field's own selection when it has one, else the selected message.
        Shortcut {
            sequences: [StandardKey.Copy]
            onActivated: {
                if (input.activeFocus && input.selectedText.length) input.copy()
                else if (!Bridge.copySelected()) Bridge.notice = "click a message first, then ctrl+c"
                if (Bridge.notice.length) Bridge.noticeTimer.restart()
            }
        }

        // ---------------------------------------------------------------- header
        Rectangle {
            id: header
            anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right
            height: 32 + Theme.fontSize; color: Theme.darkBackground
            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: Theme.border; color: Theme.muted }

            Mark { anchors.left: parent.left; anchors.leftMargin: Theme.pad + 2; anchors.verticalCenter: parent.verticalCenter; width: 22; height: 22; color: Theme.foreground }
            Text {
                x: 44; anchors.verticalCenter: parent.verticalCenter
                text: "sms"; color: Theme.brightForeground
                font.family: Theme.font; font.pixelSize: Theme.fontLarge; font.letterSpacing: 1
            }

            // link status: the phone's long-poll seen within the last ~30 s means replies leave at once
            Row {
                anchors.right: parent.right; anchors.rightMargin: Theme.pad; anchors.verticalCenter: parent.verticalCenter
                spacing: 8
                readonly property bool linked: Bridge.live && Bridge.live.link !== null && Bridge.live.link !== undefined && Bridge.live.link < 35
                Text {
                    text: Bridge.notice ? Bridge.notice : Bridge.error ? Bridge.error
                        : parent.linked ? "link · phone live" : (Bridge.live.wanted ? "queue · waiting for the phone" : "queue")
                    color: Bridge.notice ? Theme.yellow : Bridge.error ? Theme.red : (parent.linked ? Theme.green : Theme.darkForeground)
                    font.family: Theme.font; font.pixelSize: Theme.fontSmall
                    anchors.verticalCenter: parent.verticalCenter
                }
                Rectangle {
                    width: 8; height: 8; radius: Theme.radius(8)
                    color: Bridge.error ? Theme.red : (parent.linked ? Theme.green : Theme.muted)
                    anchors.verticalCenter: parent.verticalCenter
                }
                Text {   // quarantine: threads blocked on the phone or marked junk on the box
                    text: Bridge.showJunk ? "back" : ("junk · " + Bridge.junkCount)
                    color: Bridge.showJunk ? Theme.yellow : Theme.darkForeground
                    font.family: Theme.font; font.pixelSize: Theme.fontSmall
                    anchors.verticalCenter: parent.verticalCenter
                    MouseArea { anchors.fill: parent; onClicked: Bridge.showJunk = !Bridge.showJunk }
                }
                Text {   // pairing: shows the QR code the phone scans in Settings > Pair with desktop
                    text: "pair"; color: pairOverlay.visible ? Theme.accent : Theme.darkForeground
                    font.family: Theme.font; font.pixelSize: Theme.fontSmall
                    anchors.verticalCenter: parent.verticalCenter
                    MouseArea { anchors.fill: parent; onClicked: pairOverlay.toggle() }
                }
                Text {   // options: shape, bubbles, outgoing colour -- the phone's settings, mirrored
                    text: "options"; color: optionsOverlay.visible ? Theme.accent : Theme.darkForeground
                    font.family: Theme.font; font.pixelSize: Theme.fontSmall
                    anchors.verticalCenter: parent.verticalCenter
                    MouseArea { anchors.fill: parent; onClicked: optionsOverlay.visible = !optionsOverlay.visible }
                }
                Text {   // theme toggle, keyboard: D
                    text: Theme.dark ? "☽" : "☀"; color: Theme.darkForeground
                    font.family: Theme.font; font.pixelSize: Theme.fontSize
                    anchors.verticalCenter: parent.verticalCenter
                    MouseArea { anchors.fill: parent; onClicked: Theme.dark = !Theme.dark }
                }
            }
        }

        // ---------------------------------------------------------------- threads
        Rectangle {
            id: sidebar
            anchors.top: header.bottom; anchors.bottom: parent.bottom; anchors.left: parent.left
            width: Math.min(340, Math.round(win.width * 0.4)); color: Theme.background
            Rectangle { anchors.right: parent.right; height: parent.height; width: Theme.border; color: Theme.muted }

            ListView {
                id: threadList
                anchors.fill: parent; anchors.rightMargin: Theme.border
                clip: true
                model: Bridge.threads
                delegate: ThreadRow {
                    width: threadList.width
                    thread: modelData
                    current: Bridge.sameThread(modelData, Bridge.selected)
                    onClicked: Bridge.select(modelData)
                }
                ScrollBar.vertical: ScrollBar { }
            }
            Text {
                anchors.centerIn: parent; visible: Bridge.threads.length === 0
                text: Bridge.error ? Bridge.error : (Bridge.showJunk ? "nothing quarantined" : "no conversations yet")
                color: Theme.darkForeground; font.family: Theme.font; font.pixelSize: Theme.fontSize
            }
        }

        // ---------------------------------------------------------------- messages
        Item {
            id: pane
            anchors.top: header.bottom; anchors.bottom: parent.bottom
            anchors.left: sidebar.right; anchors.right: parent.right

            Rectangle {
                id: paneHeader
                anchors.top: parent.top; width: parent.width; height: 28 + Theme.fontSize; color: Theme.background
                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: Theme.border; color: Theme.muted; opacity: 0.5 }
                Text {
                    anchors.left: parent.left; anchors.leftMargin: Theme.pad + 6; anchors.verticalCenter: parent.verticalCenter
                    anchors.right: paneInfo.left; anchors.rightMargin: Theme.pad
                    text: Bridge.title(Bridge.selected); elide: Text.ElideRight
                    color: Theme.brightForeground; font.family: Theme.font; font.pixelSize: Theme.fontSize; font.bold: true
                }
                Text {
                    id: paneInfo
                    anchors.right: parent.right; anchors.rightMargin: Theme.pad + 6; anchors.verticalCenter: parent.verticalCenter
                    text: Bridge.selected ? ((Bridge.selected.name ? Bridge.pretty(Bridge.selected.addr) + " · " : "") + Bridge.selected.count + " messages") : ""
                    color: Theme.darkForeground; font.family: Theme.font; font.pixelSize: Theme.fontSmall
                }
            }

            ListView {
                id: messageList
                anchors.top: paneHeader.bottom; anchors.bottom: composer.top
                anchors.left: parent.left; anchors.right: parent.right
                anchors.margins: Theme.pad + 6
                clip: true; spacing: 0
                model: Bridge.messages
                delegate: Bubble {
                    width: messageList.width; message: modelData; maxWidth: messageList.width * 0.72
                    thread: Bridge.selected
                    previous: index > 0 ? Bridge.messages[index - 1] : null
                    next: index < Bridge.messages.length - 1 ? Bridge.messages[index + 1] : null
                }
                onCountChanged: positionViewAtEnd()
                // A text-size change reflows every bubble; stay at the newest message.
                Connections { target: Theme; function onFontSizeChanged() { Qt.callLater(messageList.positionViewAtEnd) } }
                ScrollBar.vertical: ScrollBar { }
            }
            Text {
                anchors.centerIn: messageList; visible: !Bridge.selected
                text: "pick a conversation"; color: Theme.darkForeground
                font.family: Theme.font; font.pixelSize: Theme.fontSize
            }

            // ------------------------------------------------------------ compose
            Rectangle {
                id: composer
                anchors.bottom: parent.bottom; width: parent.width; height: 44 + Theme.fontSize
                color: Theme.darkBackground; visible: !!Bridge.selected
                Rectangle { anchors.top: parent.top; width: parent.width; height: Theme.border; color: Theme.muted }

                Rectangle {
                    anchors.left: parent.left; anchors.right: sendButton.left; anchors.margins: Theme.pad
                    anchors.verticalCenter: parent.verticalCenter; height: 24 + Theme.fontSize
                    color: Theme.background; border.width: Theme.border; border.color: Theme.muted
                    radius: 0                       // the composer stays rectangular, whatever the control shape
                    TextField {
                        id: input
                        anchors.fill: parent; anchors.margins: 2
                        placeholderText: Bridge.isAgent(Bridge.selected) ? "tell " + Bridge.title(Bridge.selected) + "…" : "reply…"
                        placeholderTextColor: Theme.darkForeground
                        color: Theme.brightForeground; font.family: Theme.font; font.pixelSize: Theme.fontSize
                        background: null; leftPadding: 8
                        onAccepted: sendButton.fire()
                    }
                }
                Rectangle {
                    id: sendButton
                    anchors.right: parent.right; anchors.rightMargin: Theme.pad; anchors.verticalCenter: parent.verticalCenter
                    width: 52 + Theme.fontSize; height: 24 + Theme.fontSize; radius: 0
                    color: input.text.length ? Theme.accent : Theme.lighterBackground
                    border.width: input.text.length ? 0 : Theme.border; border.color: Theme.muted
                    property bool busy: false
                    function fire() {
                        const body = input.text.trim(); if (!body || !Bridge.selected || busy) return
                        busy = true
                        // An agent thread is addressed as its card says (chief@agents, ides@agents, or
                        // the legacy AGENTS); a person by the address their last message came from.
                        const addr = Bridge.isAgent(Bridge.selected) ? Bridge.selected.addr
                            : (Bridge.selected.in_addrs && Bridge.selected.in_addrs.length ? Bridge.selected.in_addrs[0] : Bridge.selected.addr)
                        Bridge.send(addr, body, function() { busy = false; input.text = "" })
                    }
                    Text { anchors.centerIn: parent; text: sendButton.busy ? "…" : "send"; color: input.text.length ? Theme.onAccent() : Theme.darkForeground; font.family: Theme.font; font.pixelSize: Theme.fontSize }
                    MouseArea { anchors.fill: parent; onClicked: sendButton.fire() }
                }
            }
        }

        Shortcut { sequence: "D"; onActivated: Theme.dark = !Theme.dark; context: Qt.ApplicationShortcut }
            // ---------------------------------------------------------------- options overlay
        Rectangle {
            id: optionsOverlay
            anchors.fill: parent; visible: false
            color: Qt.rgba(0, 0, 0, 0.55); z: 100
            MouseArea { anchors.fill: parent; onClicked: optionsOverlay.visible = false }
            Keys.onEscapePressed: optionsOverlay.visible = false
            onVisibleChanged: if (visible) forceActiveFocus()

            component Choice: Rectangle {
                property string label
                property bool on: false
                signal picked()
                implicitWidth: choiceText.implicitWidth + 2 * Theme.pad; implicitHeight: 17 + Theme.fontSmall
                radius: Theme.radius(height)
                color: on ? Theme.accent : Theme.lighterBackground
                border.width: on ? 0 : Theme.border; border.color: Theme.muted
                Text {
                    id: choiceText; anchors.centerIn: parent; text: parent.label
                    color: parent.on ? Theme.onAccent() : Theme.foreground
                    font.family: Theme.font; font.pixelSize: Theme.fontSmall
                }
                MouseArea { anchors.fill: parent; onClicked: parent.picked() }
            }
            component OptionRow: Column {
                id: optionRow
                property string title
                property var names: []
                property int value: 0
                signal pick(int v)
                spacing: 6
                Text { text: optionRow.title; color: Theme.darkForeground; font.family: Theme.font; font.pixelSize: Theme.fontSmall; font.letterSpacing: 1 }
                Row {
                    spacing: 6
                    Repeater {
                        model: optionRow.names
                        Choice { label: modelData; on: optionRow.value === index; onPicked: optionRow.pick(index) }
                    }
                }
            }

            Rectangle {
                anchors.centerIn: parent
                width: optionsCard.width + 4 * Theme.pad; height: optionsCard.height + 4 * Theme.pad
                color: Theme.darkBackground; radius: Theme.radius(24)
                border.width: Theme.border; border.color: Theme.muted
                MouseArea { anchors.fill: parent }
                Column {
                    id: optionsCard
                    anchors.centerIn: parent; spacing: Theme.pad * 1.6
                    Text {
                        text: "options"; color: Theme.brightForeground
                        font.family: Theme.font; font.pixelSize: Theme.fontLarge; font.letterSpacing: 1
                    }
                    OptionRow { title: "control shape"; names: Theme.shapeNames; value: Theme.shape; onPick: v => Theme.shape = v }
                    OptionRow { title: "messages"; names: ["boxes · omarchy", "bubbles · quik"]; value: Theme.bubbleStyle; onPick: v => Theme.bubbleStyle = v }
                    OptionRow { title: "outgoing messages"; names: ["app colour", "grey"]; value: Theme.outgoingAccent ? 0 : 1; onPick: v => Theme.outgoingAccent = (v === 0) }
                    OptionRow { title: "theme"; names: ["tokyo night", "flexoki light"]; value: Theme.dark ? 0 : 1; onPick: v => Theme.dark = (v === 0) }
                    OptionRow { title: "text size"; names: Theme.textSizeNames; value: Theme.textSize; onPick: v => Theme.textSize = v }
                    Text {
                        text: "saved to ~/.sms-desktop/settings.json"; color: Theme.darkForeground
                        font.family: Theme.font; font.pixelSize: Theme.fontSmall
                    }
                }
            }
        }

        // ---------------------------------------------------------------- pairing overlay
        Rectangle {
            id: pairOverlay
            anchors.fill: parent; visible: false
            color: Qt.rgba(0, 0, 0, 0.55); z: 100
            function toggle() { visible ? close() : open() }
            function open() { Bridge.makePairCode(); visible = true }
            function close() { visible = false; Bridge.dropPairCode() }
            MouseArea { anchors.fill: parent; onClicked: pairOverlay.close() }
            Keys.onEscapePressed: pairOverlay.close()
            onVisibleChanged: if (visible) forceActiveFocus()

            Rectangle {
                anchors.centerIn: parent
                width: card.width + 2 * Theme.pad * 2; height: card.height + 2 * Theme.pad * 2
                color: Theme.darkBackground; radius: Theme.radius(24)
                border.width: Theme.border; border.color: Theme.muted
                MouseArea { anchors.fill: parent }   // clicks inside do not close
                Column {
                    id: card
                    anchors.centerIn: parent; spacing: Theme.pad
                    Text {
                        text: "pair the phone"; color: Theme.brightForeground
                        font.family: Theme.font; font.pixelSize: Theme.fontLarge; font.letterSpacing: 1
                    }
                    Rectangle {   // the code always sits on white: scanners want the quiet zone light
                        width: 300; height: 300; color: "#ffffff"; radius: Theme.radius(12)
                        Image {
                            anchors.fill: parent; anchors.margins: 8
                            fillMode: Image.PreserveAspectFit; smooth: false; cache: false
                            source: Bridge.qrReady ? "file://" + Bridge.qrPath + "?v=" + Bridge.qrVersion : ""
                        }
                        Text {
                            anchors.centerIn: parent; visible: !Bridge.qrReady
                            text: Bridge.qrError || (Bridge.ready ? "…" : "no token"); color: "#100F0F"
                            font.family: Theme.font; font.pixelSize: Theme.fontSmall
                            width: 260; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter
                        }
                    }
                    Text {
                        text: "SMS & Forward › Settings › Pair with desktop"; color: Theme.foreground
                        font.family: Theme.font; font.pixelSize: Theme.fontSize
                    }
                    Text {
                        text: "fills in " + Bridge.base + "/sms and the token, and turns forwarding on"
                        color: Theme.darkForeground; font.family: Theme.font; font.pixelSize: Theme.fontSmall
                    }
                    Text {
                        text: "the code carries the bridge token: close this when done"; color: Theme.yellow
                        font.family: Theme.font; font.pixelSize: Theme.fontSmall
                    }
                }
            }
        }
    }

}
