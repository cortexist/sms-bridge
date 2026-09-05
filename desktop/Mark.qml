import QtQuick

// The app mark: the same hollow callout with two backticks for eyes as the phone's launcher
// icon, drawn from primitives so it follows the theme colour and any size.
Item {
    id: root
    property color color: Theme.foreground
    property real stroke: Math.max(1, height * 0.08)
    implicitWidth: 24; implicitHeight: 24

    Rectangle {   // the box
        id: box
        x: root.width * 0.06; y: root.height * 0.12
        width: root.width * 0.88; height: root.height * 0.62
        color: "transparent"; border.color: root.color; border.width: root.stroke
    }
    Canvas {      // the tail, filled
        anchors.fill: parent
        onPaint: {
            const c = getContext("2d"); c.reset()
            c.fillStyle = root.color
            const x0 = box.x + box.width * 0.12, y1 = box.y + box.height
            c.beginPath(); c.moveTo(x0, y1 - root.stroke); c.lineTo(x0, y1 + root.height * 0.2)
            c.lineTo(x0 + root.width * 0.2, y1 - root.stroke); c.closePath(); c.fill()
        }
        Component.onCompleted: requestPaint()
        onWidthChanged: requestPaint(); onHeightChanged: requestPaint()
    }
    Text {        // the eyes
        anchors.centerIn: box; anchors.verticalCenterOffset: -box.height * 0.04
        text: "``"; color: root.color
        font.family: Theme.font; font.bold: true; font.pixelSize: root.height * 0.7
    }
}
