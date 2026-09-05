pragma Singleton
import QtQuick
import Quickshell
import Quickshell.Io

// Design tokens shared with the phone app (SMS & Forward): the same palettes, the same
// monospace face, the same options. Tokyo Night is the dark side and Flexoki Light the
// light side, using Omarchy's semantic colour names so an Omarchy theme can be mapped
// onto these one to one later.
//
// The options mirror the phone's settings, so this bridge between two systems can lean
// either way: Omarchy's sharp boxes or QUIK's rounded bubbles, the launcher's shape or
// square, coloured or grey outgoing messages. They persist in ~/.sms-desktop/settings.json.
QtObject {
    // 0 square (default), 1 rounded, 2 round, 3 squircle -- Preferences.SHAPE_* on the phone
    property alias dark: prefs.dark
    property alias shape: prefs.shape
    property alias bubbleStyle: prefs.bubbleStyle          // 0 boxes (Omarchy), 1 bubbles (QUIK)
    property alias outgoingAccent: prefs.outgoingAccent    // your messages in the accent, else grey
    readonly property var shapeNames: ["square", "rounded", "round", "squircle"]

    property FileView settings: FileView {
        path: Quickshell.env("HOME") + "/.sms-desktop/settings.json"
        watchChanges: true
        onFileChanged: reload()
        onAdapterUpdated: writeAdapter()
        onLoadFailed: writeAdapter()       // first run: create the file with the defaults
        adapter: JsonAdapter {
            id: prefs
            property bool dark: true
            property int shape: 0
            property int bubbleStyle: 0
            property bool outgoingAccent: true
            property int textSize: 0
        }
    }
    function radius(size) { return radiusFor(shape, size) }
    function radiusFor(shape, size) {
        switch (shape) {
        case 1: return size * 0.22
        case 2: return size / 2
        case 3: return size * 0.36   // corner-radius stand-in for chips; avatars draw the true squircle (ControlShape)
        default: return 0
        }
    }

    readonly property string font: "JetBrains Mono"
    // Text size: small is the original scale; medium and large step the three roles up
    // together. Line height is the font's own (JetBrains Mono: 1.32 em), the same rule a
    // terminal uses, so at equal pixel size lines land where the terminal's do; medium is
    // pinned to this box's terminal (measured ~14.7 px, i.e. 11 pt), large is a step up.
    // Avatars keep their own size, their digits are sized from the avatar.
    property alias textSize: prefs.textSize                  // 0 small, 1 medium, 2 large
    readonly property var textSizeNames: ["small", "medium", "large"]
    readonly property int fontSize:  [12, 15, 18][textSize] || 12
    readonly property int fontSmall: [11, 14, 17][textSize] || 11
    readonly property int fontLarge: [14, 17, 21][textSize] || 14
    // One line of body text, as the font defines it: the unit the list rows are laid on.
    property FontMetrics metrics: FontMetrics { font.family: Theme.font; font.pixelSize: Theme.fontSize }
    readonly property real lineHeight: metrics.lineSpacing

    readonly property color accent:            dark ? "#7aa2f7" : "#205EA6"
    readonly property color selection:         dark ? "#292e42" : "#CECDC3"
    readonly property color muted:             dark ? "#414868" : "#B7B5AC"
    readonly property color background:        dark ? "#1a1b26" : "#FFFCF0"
    readonly property color darkBackground:    dark ? "#13141c" : "#f2efe4"
    readonly property color darkerBackground:  dark ? "#0e0e14" : "#e5e2d8"
    readonly property color lighterBackground: dark ? "#24283b" : "#E6E4D9"
    readonly property color foreground:        dark ? "#a9b1d6" : "#100F0F"
    readonly property color darkForeground:    dark ? "#565f89" : "#878580"
    readonly property color lightForeground:   dark ? "#b4bee6" : "#403E3C"
    readonly property color brightForeground:  dark ? "#c0caf5" : "#100F0F"
    readonly property color red:               dark ? "#f7768e" : "#D14D41"
    readonly property color green:             dark ? "#9ece6a" : "#879A39"
    readonly property color yellow:            dark ? "#e0af68" : "#D0A215"

    // Text on a coloured ground: whichever of near-black or white contrasts better (WCAG),
    // as on the phone. textOn() for any colour; onAccent() for the accent.
    function textOn(ground) {
        function lum(c) {
            function ch(v) { return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
            return 0.2126 * ch(c.r) + 0.7152 * ch(c.g) + 0.0722 * ch(c.b) + 0.05
        }
        const bg = lum(Qt.color(ground)), d = lum(Qt.color("#100F0F")), l = lum(Qt.color("#FFFFFF"))
        const darkRatio = Math.max(bg, d) / Math.min(bg, d), lightRatio = Math.max(bg, l) / Math.min(bg, l)
        return darkRatio >= lightRatio ? "#100F0F" : "#FFFFFF"
    }
    function onAccent() { return textOn(accent) }

    readonly property int border: 1
    readonly property int pad: 10
}
