import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// MSI fan and RGB control in the Omarchy bar.
//
// Two small helpers do the real work, and this widget only polls them:
//
//   bin/msi-fanctl  talks to msi-fan-rgbd (root) for fan curves and PWM,
//                   and falls back to plain sysfs reads when the daemon is
//                   down, so temperatures and RPM never disappear.
//   bin/msi-rgb     drives the Mystic Light controller through OpenRGB.
//
// Keeping the protocols in scripts means the QML has no hardware knowledge
// and the helpers are testable from a terminal.
BarWidget {
  id: root
  moduleName: "io.github.kamil.msi-fan-rgb"

  property var status: null
  property var rgb: null
  property string errorText: ""
  property string actionMessage: ""
  property bool popupOpen: false
  property bool rgbLoading: false

  readonly property string fanctl: decodeURIComponent(
    String(Qt.resolvedUrl("bin/msi-fanctl")).replace("file://", ""))
  readonly property string rgbctl: decodeURIComponent(
    String(Qt.resolvedUrl("bin/msi-rgb")).replace("file://", ""))

  // ------------------------------------------------------------- settings

  readonly property int intervalSec: Math.max(2, root.numberSetting("intervalSec", 5))
  readonly property string display: String(root.setting("display", "CPU temp") || "CPU temp")
  readonly property bool showRgb: root.boolSetting("showRgb", true)
  readonly property bool showUnused: root.boolSetting("showUnusedFans", false)
  readonly property string rgbDevice: String(root.setting("rgbDevice", "") || "")

  // ---------------------------------------------------------------- state

  readonly property var fans: root.status && root.status.fans ? root.status.fans : []
  readonly property var temps: root.status && root.status.temps ? root.status.temps : ({})
  readonly property bool daemonRunning: root.status ? root.status.daemon === true : false
  readonly property bool thermal: root.status ? root.status.thermal === true : false
  readonly property bool manualMode: root.status ? root.status.mode === "manual" : false
  readonly property bool rgbAvailable: root.rgb ? root.rgb.available === true : false
  readonly property bool rgbOff: root.rgb ? root.rgb.off === true : false

  readonly property real cpuTemp: {
    if (!root.status) return -1
    var value = root.status.cpuTemp
    return typeof value === "number" && value > 0 ? value : -1
  }
  readonly property int maxRpm: {
    var top = 0
    for (var i = 0; i < root.fans.length; i++) {
      var value = Number(root.fans[i].rpm || 0)
      if (value > top) top = value
    }
    return top
  }
  readonly property var visibleFans: {
    var list = []
    for (var i = 0; i < root.fans.length; i++) {
      var fan = root.fans[i]
      if (root.showUnused || Number(fan.rpm || 0) > 0 || fan.channel === 1
          || (root.manualMode && fan.controlled === true))
        list.push(fan)
    }
    return list
  }

  readonly property color normalColor: root.bar ? root.bar.barForeground : "white"
  readonly property color urgentColor: root.bar ? root.bar.urgent : "#ff5555"
  readonly property bool hot: root.thermal || root.cpuTemp >= 85

  // ----------------------------------------------------------------- bar

  readonly property string barText: {
    var parts = []
    if (root.display !== "Icon" && root.display !== "Fan RPM")
      parts.push(root.cpuTemp > 0 ? Math.round(root.cpuTemp) + "°" : "--")
    if (root.display === "Fan RPM" || root.display === "Both")
      parts.push(root.maxRpm > 0 ? String(root.maxRpm) : "--")
    return parts.length > 0 ? "󰈐 " + parts.join(" ") : "󰈐"
  }

  readonly property string tooltip: {
    if (!root.status) return "MSI fans — reading sensors"
    var parts = []
    parts.push("CPU " + (root.cpuTemp > 0 ? Math.round(root.cpuTemp) + "°C" : "—"))
    if (root.thermal) parts.push("thermal override")
    else if (root.status.profileLabel) parts.push(root.status.profileLabel)
    if (root.maxRpm > 0) parts.push("fans up to " + root.maxRpm + " RPM")
    if (root.rgb && root.rgb.available)
      parts.push("RGB " + (root.rgbOff ? "off" : (root.rgb.preset || "on")))
    if (!root.daemonRunning) parts.push("fan daemon down")
    return parts.join("  ·  ")
  }

  // ------------------------------------------------------------- helpers

  function boolSetting(key, fallback) {
    var value = root.setting(key, fallback)
    if (value === true || value === false) return value
    if (value === "true" || value === 1 || value === "1") return true
    if (value === "false" || value === 0 || value === "0") return false
    return fallback
  }

  function numberSetting(key, fallback) {
    var value = root.setting(key, fallback)
    if (typeof value === "string" && value.trim() === "") return fallback
    var number = Number(value)
    return isFinite(number) ? number : fallback
  }

  function close() {
    root.popupOpen = false
  }

  function refreshStatus() {
    if (statusProc.running) return
    statusProc.command = [root.fanctl, "status"]
    statusProc.running = true
  }

  function refreshRgb() {
    if (rgbProc.running || !root.showRgb) return
    var command = [root.rgbctl, "status"]
    if (root.rgbDevice !== "") command.push("--device", root.rgbDevice)
    rgbProc.command = command
    root.rgbLoading = true
    rgbProc.running = true
  }

  function refreshAll() {
    root.refreshStatus()
    root.refreshRgb()
  }

  function runFan(args, message) {
    if (fanAction.running) return
    root.actionMessage = message || ""
    fanAction.command = [root.fanctl].concat(args)
    fanAction.running = true
  }

  function runRgb(args, message) {
    if (rgbAction.running) return
    root.actionMessage = message || ""
    var command = [root.rgbctl]
    if (root.rgbDevice !== "") command.push("--device", root.rgbDevice)
    rgbAction.command = command.concat(args)
    rgbAction.running = true
  }

  function selectProfile(id, label) {
    if (id === "auto") root.runFan(["mode", "auto"], "Firmware curve")
    else if (id === "manual") root.runFan(["mode", "manual"], "Manual control")
    else root.runFan(["profile", id], "Profile: " + (label || id))
  }

  function setManual(channel, percent) {
    root.runFan(["manual", String(channel), String(Math.round(percent))],
                "Fan " + channel + ": " + Math.round(percent) + "%")
  }

  function applyPreset(id) {
    var args = ["preset", id]
    if (id === "static" || id === "breathing")
      args.push("--color", String(root.rgb.color || "3366FF").replace("#", ""))
    if (root.rgb && typeof root.rgb.brightness === "number")
      args.push("--brightness", String(root.rgb.brightness))
    root.runRgb(args, "RGB: " + id)
  }

  function applyColor(hex) {
    var args = ["preset", "static", "--color", hex]
    if (root.rgb && typeof root.rgb.brightness === "number")
      args.push("--brightness", String(root.rgb.brightness))
    root.runRgb(args, "RGB colour " + hex)
  }

  function applyBrightness(percent) {
    root.runRgb(["set", "--brightness", String(Math.round(percent))],
                "RGB brightness " + Math.round(percent) + "%")
  }

  function selectedPreset() {
    if (!root.rgb || !root.rgb.available) return ""
    if (root.rgbOff) return "off"
    return String(root.rgb.preset || "")
  }

  function temperatureTiles() {
    var rows = []
    var keys = [
      { key: "cpu", label: "CPU" },
      { key: "system", label: "System" },
      { key: "vrm", label: "VRM" },
      { key: "chipset", label: "Chipset" }
    ]
    for (var i = 0; i < keys.length; i++) {
      var value = root.temps[keys[i].key]
      if (typeof value !== "number" || value <= 0) continue
      rows.push({ label: keys[i].label, value: Math.round(value) + "°" })
    }
    if (rows.length === 0 && root.cpuTemp > 0)
      rows.push({ label: "CPU", value: Math.round(root.cpuTemp) + "°" })
    return rows
  }

  function profileChoices() {
    if (root.status && root.status.choices && root.status.choices.length > 0)
      return root.status.choices
    return [{ id: "auto", label: "Auto" }]
  }

  function profileSelected(id) {
    if (!root.status) return false
    var mode = root.status.mode
    if (id === "auto") return mode === "auto"
    if (id === "manual") return mode === "manual"
    return mode === "curve" && root.status.profile === id
  }

  // --------------------------------------------------------------- models

  readonly property var rgbPresets: [
    { id: "off", label: "Off" },
    { id: "static", label: "Static" },
    { id: "breathing", label: "Breathing" },
    { id: "rainbow", label: "Rainbow" },
    { id: "wave", label: "Wave" },
    { id: "meteor", label: "Meteor" }
  ]

  readonly property var swatches: [
    "FF3B30", "FF9500", "FFCC00", "34C759",
    "32ADE6", "3366FF", "AF52DE", "FF2D55"
  ]

  // ------------------------------------------------------------- processes

  Process {
    id: statusProc
    command: []
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    stderr: StdioCollector { id: statusErr; waitForEnd: true }
    onExited: function(code) {
      var out = String(statusOut.text || "").trim()
      if (code === 0 && out !== "") {
        try {
          root.status = JSON.parse(out)
          root.errorText = ""
          return
        } catch (error) {
          root.errorText = "Could not parse msi-fanctl output."
          return
        }
      }
      var message = String(statusErr.text || "").trim()
      root.errorText = message !== "" ? message
        : "msi-fanctl failed (exit " + code + ")."
    }
  }

  Process {
    id: rgbProc
    command: []
    stdout: StdioCollector { id: rgbOut; waitForEnd: true }
    stderr: StdioCollector { id: rgbErr; waitForEnd: true }
    onExited: function(code) {
      root.rgbLoading = false
      var out = String(rgbOut.text || "").trim()
      if (code === 0 && out !== "") {
        try {
          root.rgb = JSON.parse(out)
          return
        } catch (error) {
          /* fall through to the error report below */
        }
      }
      var message = String(rgbErr.text || "").trim()
      if (message !== "") root.actionMessage = message
      root.rgb = {
        available: false,
        error: message !== "" ? message : "OpenRGB not available"
      }
    }
  }

  Process {
    id: fanAction
    command: []
    stdout: StdioCollector { id: fanActionOut; waitForEnd: true }
    stderr: StdioCollector { id: fanActionErr; waitForEnd: true }
    onExited: function(code) {
      var out = String(fanActionOut.text || "").trim()
      var message = ""
      try {
        var doc = JSON.parse(out)
        if (doc && doc.ok === false) message = String(doc.error || "")
      } catch (error) {
        /* not JSON: keep the stderr report below */
      }
      if (code !== 0 && message === "")
        message = String(fanActionErr.text || "").trim()
      if (message !== "") root.actionMessage = message
      root.refreshStatus()
    }
  }

  Process {
    id: rgbAction
    command: []
    stdout: StdioCollector { id: rgbActionOut; waitForEnd: true }
    stderr: StdioCollector { id: rgbActionErr; waitForEnd: true }
    onExited: function(code) {
      var out = String(rgbActionOut.text || "").trim()
      var message = ""
      try {
        var doc = JSON.parse(out)
        if (doc && doc.ok === false) message = String(doc.error || "")
      } catch (error) {
        /* not JSON */
      }
      if (code !== 0 && message === "")
        message = String(rgbActionErr.text || "").trim()
      if (message !== "") root.actionMessage = message
      root.refreshRgb()
      root.refreshStatus()
    }
  }

  Timer {
    interval: root.intervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refreshStatus()
  }

  // OpenRGB detection is slower than reading sysfs, so it is only refreshed
  // while the popup is visible.
  Timer {
    interval: 15000
    running: root.popupOpen && root.showRgb
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refreshRgb()
  }

  // Action confirmations are useful for a moment, but should not stick around
  // in the footer forever.
  Timer {
    id: messageTimer
    interval: 6000
    onTriggered: root.actionMessage = ""
  }

  onActionMessageChanged: {
    if (root.actionMessage !== "") messageTimer.restart()
  }

  onPopupOpenChanged: {
    if (root.popupOpen) root.refreshAll()
  }

  // Scriptable from a terminal:
  //   qs ipc -p /usr/share/omarchy/shell io.github.kamil.msi-fan-rgb toggle
  IpcHandler {
    target: "io.github.kamil.msi-fan-rgb"

    function open(): void { root.popupOpen = true }
    function close(): void { root.popupOpen = false }
    function show(): void { root.popupOpen = true }
    function hide(): void { root.popupOpen = false }
    function toggle(): void { root.popupOpen = !root.popupOpen }
    function refresh(): void { root.refreshAll() }
    function status(): string {
      return JSON.stringify({ status: root.status, rgb: root.rgb,
                               error: root.errorText })
    }
    function profile(id: string): string {
      var label = id
      var items = root.profileChoices()
      for (var i = 0; i < items.length; i++) {
        if (items[i].id === id) {
          label = items[i].label
          break
        }
      }
      root.selectProfile(id, label)
      return "ok"
    }
    function manual(channel: string, percent: string): string {
      root.setManual(parseInt(channel), parseFloat(percent))
      return "ok"
    }
    function rgbPreset(id: string): string {
      root.applyPreset(id)
      return "ok"
    }
    function rgbColor(hex: string): string {
      root.applyColor(String(hex).replace("#", ""))
      return "ok"
    }
    function rgbBrightness(percent: string): string {
      root.applyBrightness(parseFloat(percent))
      return "ok"
    }
  }

  // ------------------------------------------------------------------ bar

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barText
    fontSize: Style.font.caption
    tooltipText: root.tooltip
    active: root.hot
    foreground: root.hot ? root.urgentColor : root.normalColor
    onPressed: function(mouse) {
      if (mouse === Qt.LeftButton) root.popupOpen = !root.popupOpen
    }
  }

  // ---------------------------------------------------------------- popup

  PopupCard {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.popupOpen
    contentWidth: popup.fittedContentWidth(Style.space(360))
    contentHeight: popup.fittedContentHeight(body.implicitHeight)

    Column {
      id: body
      anchors.fill: parent
      spacing: Style.space(8)

      // Header ------------------------------------------------------------
      Row {
        width: parent.width
        spacing: Style.space(10)

        Text {
          textFormat: Text.PlainText
          text: "󰈐"
          color: root.hot ? root.urgentColor : root.normalColor
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.displayLarge
          anchors.verticalCenter: parent.verticalCenter
        }

        Column {
          width: parent.width - Style.space(34)
          spacing: Style.space(1)
          anchors.verticalCenter: parent.verticalCenter

          Text {
            textFormat: Text.PlainText
            text: "MSI Fan & RGB"
            color: root.normalColor
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.subtitle
            font.bold: true
            elide: Text.ElideRight
            width: parent.width
          }

          Text {
            textFormat: Text.PlainText
            text: {
              if (!root.status) return root.errorText !== "" ? root.errorText : "Reading sensors…"
              if (root.thermal) return "Thermal override · all fans at 100%"
              if (!root.daemonRunning) return "Fan daemon not running · read-only"
              return root.status.profileLabel + " · " + (root.cpuTemp > 0
                ? Math.round(root.cpuTemp) + "°C" : "no CPU temperature")
            }
            color: root.hot ? root.urgentColor : Qt.darker(root.normalColor, 1.3)
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
            width: parent.width
          }
        }
      }

      // Temperatures ------------------------------------------------------
      Row {
        width: parent.width
        spacing: Style.space(6)

        Repeater {
          model: root.temperatureTiles()

          Column {
            required property var modelData
            width: (body.width - Style.space(18)) / 4
            spacing: Style.space(1)

            Text {
              textFormat: Text.PlainText
              text: modelData.value
              color: root.normalColor
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.body
              font.bold: true
            }

            Text {
              textFormat: Text.PlainText
              text: modelData.label
              color: Qt.darker(root.normalColor, 1.5)
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.caption
            }
          }
        }
      }

      PanelSeparator { foreground: root.normalColor }

      // Fan profile -------------------------------------------------------
      PanelSectionHeader {
        text: "Fan profile"
        foreground: root.normalColor
      }

      Flow {
        width: parent.width
        spacing: Style.space(5)

        Repeater {
          model: root.profileChoices()

          Button {
            required property var modelData
            text: modelData.label
            selected: root.profileSelected(modelData.id)
            enabled: root.daemonRunning && !fanAction.running
            opacity: enabled ? 1.0 : 0.45
            foreground: root.normalColor
            onClicked: root.selectProfile(modelData.id, modelData.label)
          }
        }
      }

      // Fans --------------------------------------------------------------
      PanelSectionHeader {
        text: "Fans"
        foreground: root.normalColor
      }

      Column {
        width: parent.width
        spacing: Style.space(5)

        Repeater {
          model: root.visibleFans

          Column {
            required property var modelData
            width: parent.width
            spacing: Style.space(2)

            Row {
              width: parent.width
              spacing: Style.space(6)

              Text {
                textFormat: Text.PlainText
                text: modelData.label
                color: root.normalColor
                font.family: root.bar ? root.bar.fontFamily : Style.font.family
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideRight
                width: parent.width - fanValue.width - Style.space(6)
              }

              Text {
                id: fanValue
                textFormat: Text.PlainText
                text: modelData.rpm + " RPM · " + modelData.percent + "%"
                color: Qt.darker(root.normalColor, 1.4)
                font.family: root.bar ? root.bar.fontFamily : Style.font.family
                font.pixelSize: Style.font.caption
              }
            }

            PanelSlider {
              visible: root.manualMode
              width: parent.width
              bar: root.bar
              minimum: 0
              maximum: 100
              integer: true
              step: 1
              value: modelData.percent
              enabled: root.daemonRunning && !fanAction.running
              onReleased: function(percent) {
                root.setManual(modelData.channel, percent)
              }
            }

            Rectangle {
              visible: !root.manualMode
              width: parent.width
              height: Math.max(2, Style.space(3))
              radius: height / 2
              color: Qt.rgba(root.normalColor.r, root.normalColor.g,
                             root.normalColor.b, 0.18)

              Rectangle {
                width: parent.width * Math.max(0, Math.min(1, modelData.percent / 100))
                height: parent.height
                radius: parent.radius
                color: root.manualMode ? root.urgentColor : root.normalColor
                opacity: 0.75
              }
            }
          }
        }
      }

      // RGB ----------------------------------------------------------------
      PanelSeparator {
        visible: root.showRgb
        foreground: root.normalColor
      }

      Column {
        visible: root.showRgb
        width: parent.width
        spacing: Style.space(6)

        PanelSectionHeader {
          text: root.rgbAvailable ? "RGB · " + (root.rgb.device || "Mystic Light")
            : "RGB"
          foreground: root.normalColor
          width: parent.width
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          visible: !root.rgbAvailable
          text: root.rgbLoading ? "Checking OpenRGB…"
            : (root.rgb && root.rgb.error ? root.rgb.error
              : "OpenRGB not available")
          color: Qt.darker(root.normalColor, 1.5)
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.caption
          width: parent.width
          wrapMode: Text.WordWrap
        }

        Flow {
          visible: root.rgbAvailable
          width: parent.width
          spacing: Style.space(5)

          Repeater {
            model: root.rgbPresets

            Button {
              required property var modelData
              text: modelData.label
              selected: root.selectedPreset() === modelData.id
              enabled: root.rgbAvailable && !rgbAction.running
              opacity: enabled ? 1.0 : 0.45
              foreground: root.normalColor
              onClicked: root.applyPreset(modelData.id)
            }
          }
        }

        Row {
          visible: root.rgbAvailable
          width: parent.width
          spacing: Style.space(5)

          Repeater {
            model: root.swatches

            Rectangle {
              required property string modelData
              width: Style.space(22)
              height: width
              radius: Style.cornerRadius > 0 ? width / 2 : 2
              color: "#" + modelData
              border.width: (String(root.rgb && root.rgb.color
                ? root.rgb.color : "").replace("#", "").toUpperCase() === modelData)
                ? Math.max(2, Style.space(2)) : 1
              border.color: border.width > 1 ? root.normalColor
                : Qt.rgba(root.normalColor.r, root.normalColor.g,
                          root.normalColor.b, 0.35)

              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.applyColor(modelData)
              }
            }
          }
        }

        Row {
          visible: root.rgbAvailable
          width: parent.width
          spacing: Style.space(6)

          Text {
            id: brightnessLabel
            textFormat: Text.PlainText
            text: "Brightness"
            color: Qt.darker(root.normalColor, 1.4)
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
          }

          Item {
            width: Math.max(0, parent.width - brightnessLabel.implicitWidth
              - brightnessValue.implicitWidth - Style.space(12))
            height: 1
          }

          Text {
            id: brightnessValue
            textFormat: Text.PlainText
            text: (root.rgb && typeof root.rgb.brightness === "number"
              ? Math.round(root.rgb.brightness) : 100) + "%"
            color: Qt.darker(root.normalColor, 1.4)
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
          }
        }

        PanelSlider {
          visible: root.rgbAvailable
          width: parent.width
          bar: root.bar
          minimum: 0
          maximum: 100
          integer: true
          step: 1
          value: root.rgb && typeof root.rgb.brightness === "number"
            ? Math.max(0, Math.min(100, root.rgb.brightness)) : 100
          enabled: root.rgbAvailable && !rgbAction.running
          onReleased: function(percent) { root.applyBrightness(percent) }
        }
      }

      // Footer -------------------------------------------------------------
      Text {
        textFormat: Text.PlainText
        text: {
          if (root.actionMessage !== "") return root.actionMessage
          var parts = []
          if (root.status) {
            parts.push(root.status.driver || "nct6687")
            parts.push(root.daemonRunning ? "daemon running" : "daemon down")
            if (root.status.curveSource) parts.push("curve source: " + root.status.curveSource)
          }
          return parts.join("  ·  ")
        }
        color: Qt.darker(root.normalColor, 1.7)
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
        width: parent.width
      }

      Text {
        textFormat: Text.PlainText
        visible: !root.daemonRunning && root.status !== null
        text: "Fan control needs msi-fan-rgbd. Install it from the plugin README."
        color: Qt.darker(root.normalColor, 1.5)
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
        width: parent.width
      }
    }
  }
}
