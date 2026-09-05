import QtQuick
import QtQuick.Shapes

// The control shape, drawn as geometry rather than a corner radius, so it matches the
// phone's ControlShapeDrawable exactly: square, rounded square (radius 22% of the shorter
// side), circle, or One UI's squircle -- four cubic Béziers from one side's midpoint to
// the next with both control points 70% of the way along the edges, from Samsung's SVG.
Item {
    id: root
    property int shape: Theme.shape
    property color color: "#000000"

    readonly property string svg: {
        const w = width, h = height, cx = w / 2, cy = h / 2, rx = w / 2, ry = h / 2
        switch (shape) {
        case 1: {   // rounded
            const r = Math.min(w, h) * 0.22
            return "M " + r + " 0 H " + (w - r) + " A " + r + " " + r + " 0 0 1 " + w + " " + r
                 + " V " + (h - r) + " A " + r + " " + r + " 0 0 1 " + (w - r) + " " + h
                 + " H " + r + " A " + r + " " + r + " 0 0 1 0 " + (h - r)
                 + " V " + r + " A " + r + " " + r + " 0 0 1 " + r + " 0 Z"
        }
        case 2:     // round
            return "M 0 " + cy + " A " + rx + " " + ry + " 0 1 1 " + w + " " + cy
                 + " A " + rx + " " + ry + " 0 1 1 0 " + cy + " Z"
        case 3: {   // squircle
            const kx = rx * 0.7, ky = ry * 0.7
            return "M 0 " + cy
                 + " C 0 " + (cy - ky) + " " + (cx - kx) + " 0 " + cx + " 0"
                 + " C " + (cx + kx) + " 0 " + w + " " + (cy - ky) + " " + w + " " + cy
                 + " C " + w + " " + (cy + ky) + " " + (cx + kx) + " " + h + " " + cx + " " + h
                 + " C " + (cx - kx) + " " + h + " 0 " + (cy + ky) + " 0 " + cy + " Z"
        }
        default:    // square
            return "M 0 0 H " + w + " V " + h + " H 0 Z"
        }
    }

    Shape {
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeWidth: -1
            fillColor: root.color
            PathSvg { path: root.svg }
        }
    }
}
