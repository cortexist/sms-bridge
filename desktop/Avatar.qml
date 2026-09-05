import QtQuick
import QtQuick.Effects

// A conversation avatar: the control shape, filled with a per-thread tint. The contact's
// photo when the card has one (fetched from the bridge, so it works from any machine); otherwise
// initials -- or, for a bare number, its last two digits, the part people remember --
// embossed in whichever of near-black or white contrasts with the tint. AGENTS get the
// face. A red badge marks a failed last send, as on the phone.
Item {
    id: root
    property var thread
    property bool failed: false
    implicitWidth: 40; implicitHeight: 40
    // Every avatar follows the shape setting, agents included: their colour is their
    // identity, the shape is the user's. Drawn as geometry (ControlShape) so the squircle
    // is the phone's, not a corner-radius approximation.
    readonly property color color: Bridge.tint(thread)
    readonly property string photoSource: Bridge.photoSource(root.thread)
    readonly property bool hasPhoto: photoSource.length > 0
    readonly property color ink: Theme.textOn(root.color)

    ControlShape { anchors.fill: parent; color: root.color }

    // The photo, clipped to the shape: the shape is a mask, since clip: true is rectangular.
    Image {
        id: photo
        anchors.fill: parent
        source: root.photoSource
        fillMode: Image.PreserveAspectCrop
        sourceSize: Qt.size(96, 96)
        asynchronous: true
        visible: false
        layer.enabled: root.hasPhoto
    }
    Item {
        id: shapeMask
        anchors.fill: parent
        visible: false
        layer.enabled: root.hasPhoto
        ControlShape { anchors.fill: parent; color: "#000000" }
    }
    MultiEffect {
        anchors.fill: parent
        visible: root.hasPhoto && photo.status === Image.Ready
        source: photo
        maskEnabled: true
        maskSource: shapeMask
        maskThresholdMin: 0.5
        maskSpreadAtMin: 1.0
    }

    // Initials or digits, embossed: a light edge up-left, a dark edge down-right, ink on top.
    Item {
        anchors.fill: parent
        visible: !root.hasPhoto || photo.status !== Image.Ready
        readonly property string label: Bridge.initials(root.thread)
        readonly property bool agent: Bridge.isAgent(root.thread)
        readonly property real size: agent ? root.height * 0.62 : root.height * 0.38
        readonly property real dy: agent ? root.height * 0.08 : 0
        Repeater {
            model: [
                { dx: -1, dy: -1, color: Qt.rgba(1, 1, 1, 0.35) },
                { dx: 1, dy: 1, color: Qt.rgba(0, 0, 0, 0.45) },
                { dx: 0, dy: 0, color: root.ink }
            ]
            Text {
                anchors.centerIn: parent
                anchors.horizontalCenterOffset: modelData.dx
                anchors.verticalCenterOffset: parent.dy + modelData.dy
                text: parent.label
                color: modelData.color
                font.family: Theme.font; font.bold: true
                font.pixelSize: parent.size
            }
        }
    }
    Rectangle {
        visible: root.failed
        width: root.width * 0.42; height: width
        anchors.right: parent.right; anchors.bottom: parent.bottom
        anchors.margins: -width * 0.12
        radius: Theme.radius(width); color: Theme.red
        Text { anchors.centerIn: parent; text: "!"; color: "#FFFFFF"; font.family: Theme.font; font.bold: true; font.pixelSize: parent.height * 0.75 }
    }
}
