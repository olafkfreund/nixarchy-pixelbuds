import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Services.Mpris
import Quickshell.Services.Pipewire
import "Model.js" as Model

// One instance per shell (manifest kind "service"), shared by the bar widget
// on every monitor, so there is exactly one bridge and one Maestro session
// no matter how many bars show the widget.
//
// The bridge (bridge/pixelbuds_bridge.py) is started only on BlueZ connect
// events, holds a single RFCOMM session while the buds stay connected and
// exits on its own when they leave. Commands go to it as JSON lines on
// stdin; state comes back as bounded JSON lines on stdout. No shell is ever
// spawned, every launch is an absolute path with an argv array and a closed,
// allow-listed environment.
Item {
  id: root

  property var shell: null
  property var manifest: null

  visible: false
  implicitWidth: 0
  implicitHeight: 0

  // Distro identities. Never ambient PATH.
  readonly property string python3Bin: "/usr/bin/python3"
  readonly property string gdbusBin: "/usr/bin/gdbus"
  readonly property string trustedPath: "/usr/bin:/bin"
  readonly property string bridgePath: Model.localPath(Qt.resolvedUrl("bridge/pixelbuds_bridge.py"))

  // Device state, already sanitized (Model.sanitizeStatus / sanitizeControls).
  property var status: ({})
  property var controls: ({})
  property string pendingAnc: ""
  property bool ready: false
  property bool readingControls: false
  property bool controlsRead: false     // a full controls read completed this session
  readonly property bool connected: String(status.connected || "0") === "1"
  readonly property bool adaptiveSupported: String(status.adaptive_supported || "0") === "1"
  readonly property var ancModes: adaptiveSupported ? Model.ANC_MODES : Model.LEGACY_ANC_MODES

  // ---- ear detection / auto-pause ----
  // The user's choice (persisted by the bridge in the plugin's state dir;
  // default on, like Pixel Buds on Android). Off = no head watch, no pausing.
  property bool autoPause: true
  readonly property bool ohdOff: controls.ctl_ohd === "false"
  readonly property bool headWatchWanted: autoPause && !ohdOff
  // Last on/off-head report. Kept across a bridge restart after a dropped
  // link, so the new session's first snapshot is compared with it.
  property var head: null
  property var lastWorn: null
  property var pausedPlayers: []
  property bool _headWatchSent: false
  // Act only when these buds are the default output: never pause speakers.
  readonly property var audioSink: Pipewire.defaultAudioSink
  readonly property bool budsAreOutput: connected && !!audioSink && !!status.addr
      && String(audioSink.name || "").indexOf(String(status.addr).replace(/:/g, "_")) >= 0

  property string _buf: ""
  property int _nextId: 1
  property bool _stopping: false
  property string _byeReason: ""
  property int _failures: 0
  property double _lastRefresh: 0

  // Allowlist only. clearEnvironment drops PYTHON*, LD_* and ambient PATH
  // before /usr/bin/python3 starts; the bridge needs only the system bus
  // address (if non-default) and the XDG dirs for its lock and cache.
  function launchEnvironment() {
    var env = {
      PATH: root.trustedPath,
      HOME: Quickshell.env("HOME") || "",
      LANG: Quickshell.env("LANG") || "C.UTF-8"
    }
    var pass = ["USER", "LOGNAME", "XDG_RUNTIME_DIR", "XDG_STATE_HOME",
                "DBUS_SYSTEM_BUS_ADDRESS", "LC_ALL", "LC_CTYPE"]
    for (var i = 0; i < pass.length; i++) {
      var value = Quickshell.env(pass[i])
      if (value !== undefined && value !== null && String(value) !== "")
        env[pass[i]] = String(value)
    }
    return env
  }

  // ---- bridge lifecycle ----

  function ensureBridge() {
    if (bridge.running || _stopping) return
    restartTimer.stop()
    _buf = ""
    _byeReason = ""
    ready = false
    controlsRead = false
    bridge.environment = root.launchEnvironment()
    bridge.running = true
  }

  // User disconnect or protocol violation: stop talking to the buds now.
  // The bridge shuts its RFCOMM socket on SIGTERM; SIGKILL follows if it
  // has not exited within 1.5 s.
  function stopBridge() {
    restartTimer.stop()
    ready = false
    readingControls = false
    pendingAnc = ""
    if (bridge.running) {
      _stopping = true
      bridge.running = false
      killTimer.restart()
    }
  }

  function abort() {
    stopBridge()
    status = ({})
    controls = ({})
    forgetHead()
  }

  function forgetHead() {
    head = null
    lastWorn = null
    forgetPaused()
  }

  function onBridgeExit(code) {
    killTimer.stop()
    var stopped = _stopping
    _stopping = false
    ready = false
    readingControls = false
    pendingAnc = ""
    _headWatchSent = false
    if (stopped) return
    var reason = _byeReason !== "" ? _byeReason : (code === 0 ? "stdin_closed" : "crashed")
    if (Model.retryable(reason) && connected) {
      _failures++
      if (_failures <= 5) {
        status = Object.assign({}, status, { error: "Reconnecting to the buds…" })
        restartTimer.interval = Model.retryDelay(_failures)
        restartTimer.restart()
        return
      }
      status = Object.assign({}, status, { error: "Could not reach the buds' control service." })
      return
    }
    // absent / not_ready / disconnected: wait for the next BlueZ event.
    status = ({})
    controls = ({})
    forgetHead()
  }

  function onChunk(chunk) {
    var split = Model.splitLines(_buf, chunk)
    _buf = split.rest
    for (var i = 0; i < split.lines.length; i++) {
      if (!handleLine(split.lines[i])) { split.overflow = true; break }
    }
    if (split.overflow) {
      // Protocol violation: stop the session and drop whatever it said.
      _buf = ""
      abort()
    }
  }

  function handleLine(line) {
    if (line === "") return true
    var ev = Model.parseEvent(line)
    if (ev === null) return false
    switch (ev.type) {
      case "hello":
        return ev.v === Model.PROTOCOL_VERSION
      case "state":
        status = Model.sanitizeStatus(ev.status)
        if (pendingAnc !== "" && status.anc === pendingAnc) pendingAnc = ""
        return true
      case "controls":
        controls = Model.sanitizeControls(ev.controls)
        return true
      case "ready":
        ready = true
        _failures = 0
        syncHeadWatch()
        return true
      case "prefs":
        if (typeof ev.auto_pause === "boolean") autoPause = ev.auto_pause
        return true
      case "head":
        if (typeof ev.left !== "boolean" || typeof ev.right !== "boolean") return false
        head = { left: ev.left, right: ev.right }
        evaluateHead()
        return true
      case "result":
        if (ev.cmd === "set_anc" || ev.cmd === "cycle_anc") { if (ev.ok !== true) pendingAnc = "" }
        if (ev.cmd === "controls") {
          readingControls = false
          if (ev.ok === true) controlsRead = true
        }
        return true
      case "bye":
        _byeReason = typeof ev.reason === "string" ? ev.reason.substring(0, 32) : "error"
        return true
      case "error":
        return true
      default:
        return true
    }
  }

  function send(cmd) {
    if (!bridge.running || !ready) return false
    cmd.id = _nextId
    _nextId = _nextId >= 2147483646 ? 1 : _nextId + 1
    bridge.write(JSON.stringify(cmd) + "\n")
    return true
  }

  // ---- ear detection ----

  function syncHeadWatch() {
    if (!ready) return
    if (headWatchWanted === _headWatchSent) return
    if (send({ cmd: "head_watch", on: headWatchWanted })) _headWatchSent = headWatchWanted
    if (!headWatchWanted) forgetHead()
  }
  onHeadWatchWantedChanged: syncHeadWatch()

  // Android semantics: either bud leaving the ear (or going into the case)
  // pauses what is playing; both back in resumes what we paused.
  function evaluateHead() {
    if (!headWatchWanted) return
    var cur = Model.worn(head, status)
    if (cur === null) return
    var change = Model.headTransition(lastWorn, cur)
    lastWorn = cur
    if (change === "off") pausePlaying()
    else if (change === "on") resumePaused()
  }
  onStatusChanged: evaluateHead()

  function livePlayers() {
    return Mpris.players ? Mpris.players.values : []
  }

  function pausePlaying() {
    if (!autoPause || !budsAreOutput) return
    var list = livePlayers()
    var paused = pausedPlayers.slice()
    for (var i = 0; i < list.length; i++) {
      var p = list[i]
      if (!p || !p.isPlaying || !p.canPause) continue
      p.pause()
      if (paused.indexOf(p) < 0) paused.push(p)
    }
    pausedPlayers = paused
    if (paused.length > 0) pauseExpiry.restart()
  }

  function resumePaused() {
    var list = pausedPlayers
    forgetPaused()
    if (!autoPause || !budsAreOutput) return
    var live = livePlayers()
    for (var i = 0; i < list.length; i++) {
      var p = list[i]
      // Only players that still exist and are still paused by us.
      if (live.indexOf(p) < 0 || p.playbackState !== MprisPlaybackState.Paused || !p.canPlay) continue
      p.play()
    }
  }

  function forgetPaused() {
    pauseExpiry.stop()
    if (pausedPlayers.length > 0) pausedPlayers = []
  }

  function dropPlayer(p) {
    var i = pausedPlayers.indexOf(p)
    if (i < 0) return
    var next = pausedPlayers.slice()
    next.splice(i, 1)
    pausedPlayers = next
  }

  // The user took over (played or stopped it themselves): never resume it.
  Instantiator {
    model: root.pausedPlayers
    delegate: Connections {
      required property var modelData
      target: modelData
      function onPlaybackStateChanged() {
        if (modelData.playbackState !== MprisPlaybackState.Paused) root.dropPlayer(modelData)
      }
    }
  }

  Timer {
    id: pauseExpiry
    interval: 10 * 60 * 1000
    onTriggered: root.forgetPaused()
  }

  PwObjectTracker { objects: root.audioSink ? [root.audioSink] : [] }

  function setAutoPause(on) {
    on = on === true
    if (on === autoPause) return
    if (!send({ cmd: "set_pref", key: "auto_pause", value: on })) return
    autoPause = on
  }

  // ---- API used by Panel.qml ----

  function refresh() {
    if (!bridge.running) {
      if (connected && !restartTimer.running) ensureBridge()
      return
    }
    var now = Date.now()
    if (now - _lastRefresh < 2000) return    // several bars poll; one read is enough
    _lastRefresh = now
    send({ cmd: "refresh" })
  }

  function refreshControls() {
    if (send({ cmd: "controls" })) readingControls = true
  }

  function setAnc(mode) {
    if (ancModes.indexOf(mode) < 0) return
    if (send({ cmd: "set_anc", mode: mode })) pendingAnc = mode
  }

  function cycleAnc(delta) {
    // The buds cycle their own configured hold-gesture loop, so a click and
    // a physical long press always agree.
    pendingAnc = ""
    send({ cmd: "cycle_anc", direction: delta < 0 ? "prev" : "next" })
  }

  // key is one of the bridge's setting names; fields carry typed values,
  // e.g. { value: true }, { left: "anc", right: "assistant" },
  // { modes: ["off", "aware"] }, { value: -20 }, { bands: [0, 0, 0, 0, 0] }.
  function setControl(key, fields) {
    var cmd = { cmd: "set", key: String(key) }
    for (var k in fields) cmd[k] = fields[k]
    send(cmd)
  }

  // ---- processes ----

  Process {
    id: bridge
    command: [root.python3Bin, "-I", "-B", root.bridgePath]
    clearEnvironment: true
    environment: ({})
    stdinEnabled: true
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(chunk) { root.onChunk(chunk) }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(chunk) {}
    }
    onExited: function(code) { root.onBridgeExit(code) }
  }

  Timer {
    id: killTimer
    interval: 1500
    onTriggered: if (bridge.running) bridge.signal(9)
  }

  Timer {
    id: restartTimer
    interval: 1000
    onTriggered: root.ensureBridge()
  }

  // Connect/disconnect is event-driven: a plain signal subscription on the
  // system bus (no BecomeMonitor, no privileges). gdbus prints one line per
  // signal, beginning with the object path.
  Process {
    id: bluezMonitor
    command: [root.gdbusBin, "monitor", "--system", "--dest", "org.bluez"]
    clearEnvironment: true
    environment: ({})
    stdout: SplitParser {
      onRead: function(line) {
        if (line.length > 4096) return
        var leaving = line.indexOf("'Connected': <false>") >= 0
            || line.indexOf("'ServicesResolved': <false>") >= 0
        if (leaving) {
          // A device is going away. If it is ours (or we cannot tell), stop
          // the session immediately so nothing of ours holds the link open.
          var ours = root.status.addr ? "dev_" + String(root.status.addr).replace(/:/g, "_") : ""
          var path = line.split(":")[0]
          if (ours === "" || path.indexOf("/org/bluez/") !== 0 || path.indexOf(ours) >= 0) {
            root.abort()
            root._failures = 0
          }
          eventDebounce.restart()
        } else if (line.indexOf("'Connected'") >= 0 || line.indexOf("'ServicesResolved'") >= 0) {
          root._failures = 0
          eventDebounce.restart()
        }
      }
    }
    onExited: monitorRestart.start()
  }

  Timer {
    id: monitorRestart
    interval: 3000
    onTriggered: {
      bluezMonitor.environment = root.launchEnvironment()
      bluezMonitor.running = true
    }
  }

  Timer {
    id: eventDebounce
    interval: 400
    onTriggered: root.ensureBridge()
  }

  Component.onCompleted: {
    bluezMonitor.environment = root.launchEnvironment()
    bluezMonitor.running = true
    ensureBridge()
  }

  Component.onDestruction: {
    restartTimer.stop()
    if (bridge.running) bridge.running = false
  }
}
