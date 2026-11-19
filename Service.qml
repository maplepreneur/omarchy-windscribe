import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import "Model.js" as Model

// Windscribe state for the bar widget.
//
// One poll with a small cost drives everything:
//
//   * `windscribe-cli status` (~40ms, a compiled binary talking to the
//     engine's local socket) runs on a short interval and owns the bar
//     icon, the login state, the firewall, the data usage, and the
//     connection detail. Proton's widget needed two polls because its
//     CLI costs a second of Python start-up; this one doesn't, so a
//     single probe answers "are we up, are we signed in, and where".
//
// `windscribe-cli connect` blocks for up to a minute and a half, so every
// action is optimistic: _desired pins the UI to the requested state
// until reality agrees.
//
// Every subprocess is an argv list. The one place a value crosses a shell
// boundary is sign-in, where the username is whitelisted and single-quoted
// before it reaches the terminal launcher.
Item {
  id: root

  property var settings: ({})
  property bool panelOpen: false

  property bool installed: false
  property bool installing: false

  property bool signedIn: false
  // The status probe costs ~40ms, so signedIn is false-but-unknown only
  // until the first probe lands. Without this the widget briefly claims
  // "Signed out" over a tunnel that is plainly up.
  property bool accountProbed: false
  property string loginError: ""

  // status-derived
  property bool statusConnected: false
  property bool statusConnecting: false
  property bool statusDisconnecting: false
  property string currentWhere: ""        // "Toronto - Comfort Zone"
  property string currentCity: ""         // "Toronto"
  property string currentNickname: ""     // "Comfort Zone"
  property bool checkingConnection: false
  property bool interference: false
  property string statusText: "Checking…"
  property string protocolLine: ""
  property string ipLine: ""
  property string firewallState: ""      // "On" | "Off" | "Always On" | ""
  property string dataUsage: ""
  property string updateAvailable: ""
  property var fields: []

  // Regions with their cities, from `windscribe-cli locations`.
  property var regions: []
  property bool regionsLoaded: false
  // City name (lowercased) -> region name, so a map dot can drill into
  // its region. Built whenever the region list lands.
  property var cityRegion: ({})
  // The account's static IPs, from `locations static`.
  property var statics: []
  property bool staticsLoaded: false

  // Every Windscribe city with coordinates, for the mini-map. Bundled
  // offline in places.py; a city it doesn't know is simply not lit.
  property var cities: []
  property bool citiesLoaded: false
  // {city, lat, lon} for the connected place, or null.
  property var currentPlace: null
  property string _locatePending: ""

  // Tunnel throughput, from the kernel's own counters for the tunnel
  // interface (/sys/class/net/<dev>/statistics). The engine names its
  // interface utun<NNN>; it is discovered on connect and sampled once a
  // second, only while the panel is open and a tunnel is up.
  property string linkDevice: ""
  property var rxHistory: []
  property var txHistory: []
  property real rxRate: 0
  property real txRate: 0
  property real sessionRx: 0
  property real sessionTx: 0
  property int uptimeSec: 0
  property real _lastRx: -1
  property real _lastTx: -1
  property real _lastSampleMs: 0
  property real _linkUpMs: 0
  readonly property int trafficSamples: 60

  // ── Windscribe's preferences file ────────────────────────────────────
  // ~/.config/Windscribe/windscribe_cli.conf is an INI file the engine
  // watches and applies "as soon as the file is saved". The CLI has no
  // command for split tunneling, LAN traffic or the always-on firewall
  // mode, so those keys are edited here, with the same discipline
  // OmaProton applies to Proton's settings.json: read it fresh, change
  // one value, hand every other byte back exactly as found, and never
  // create the file.
  readonly property string confPath:
    (Quickshell.env("XDG_CONFIG_HOME") || (Quickshell.env("HOME") + "/.config"))
    + "/Windscribe/windscribe_cli.conf"
  property var confConnection: null      // parsed [Connection] or null
  property bool confLoaded: false
  property bool confMissing: false
  property bool confError: false

  readonly property bool splitAvailable: confLoaded && confConnection !== null
  readonly property bool splitOn: splitAvailable && String(confConnection["SplitTunnelingEnabled"] || "false") === "true"
  readonly property string splitMode: {
    var m = String((confConnection || {})["SplitTunnelingMode"] || "Exclude")
    return m === "Include" ? "Include" : "Exclude"
  }
  readonly property var splitApps: {
    var raw = String((confConnection || {})["SplitTunnelingApps"] || "")
    var out = []
    var seen = ({})
    var parts = raw.split(",")
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i].trim()
      if (p.charAt(0) !== "/" || seen[p]) continue
      seen[p] = true
      out.push(p)
    }
    return out
  }
  // The firewall's mode, from the engine's own file. "Auto" is the
  // shipped default: the firewall comes up before the tunnel and goes
  // down with it. "Always On" is a true kill switch: it never goes down,
  // and the CLI then refuses `firewall off`.
  readonly property string firewallMode: String((confConnection || {})["FirewallMode"] || "")
  readonly property bool firewallAlwaysOn: firewallMode === "Always On"
  readonly property bool lanTrafficOn: splitAvailable && String(confConnection["AllowLANTraffic"] || "false") === "true"

  // Only values writeConf() will ever hand to the file.
  readonly property var confValues: ({
    "FirewallMode": ["Auto", "Always On"],
    "AllowLANTraffic": ["true", "false"],
    "SplitTunnelingEnabled": ["true", "false"],
    "SplitTunnelingMode": ["Exclude", "Include"]
  })

  function writeConf(key, value) {
    if (!splitAvailable) return false
    var allowed = confValues[key]
    if (!allowed || allowed.indexOf(value) === -1) return false
    // The engine rewrites this file itself, so never write across one of
    // our own actions; a CLI run that changes settings and a save here
    // would race on the same file.
    if (actionProcess.running || connectProcess.running) return false
    var raw = confFile.text()
    var next = Model.setConfValue(raw, "Connection", key, value)
    if (next === "") { confError = true; return false }
    confError = false
    // Read-modify-write against the text we just parsed, not a re-parse:
    // the engine may have saved between the parse and now, and losing
    // that change would be worse than waiting for the next read. The
    // FileView watcher re-reads whatever lands on disk anyway.
    confFile.setText(next)
    return true
  }

  function toggleSplitTunnel() { writeConf("SplitTunnelingEnabled", splitOn ? "false" : "true") }
  function setSplitMode(mode) {
    if (mode !== "Exclude" && mode !== "Include") return
    if (mode === splitMode) return
    writeConf("SplitTunnelingMode", mode)
  }
  // The panel hands back the full selection rather than one add or remove,
  // which is what MultiSelect emits and what the conf stores anyway.
  // Paths are only ever accepted from the installed-app scan, so a path
  // that isn't absolute never reaches the file.
  function setSplitApps(paths) {
    if (!splitAvailable) return
    var clean = []
    var seen = ({})
    var arr = (paths && typeof paths.length === "number") ? paths : []
    for (var i = 0; i < arr.length; i++) {
      var p = String(arr[i])
      if (p.charAt(0) !== "/" || p.indexOf("\u0000") !== -1) continue
      if (seen[p]) continue
      seen[p] = true
      clean.push(p)
    }
    var raw = confFile.text()
    var next = Model.setConfValue(raw, "Connection", "SplitTunnelingApps", clean.join(","))
    if (next !== "") confFile.setText(next)
  }
  function toggleLanTraffic() { writeConf("AllowLANTraffic", lanTrafficOn ? "false" : "true") }
  function toggleAlwaysOnFirewall() { writeConf("FirewallMode", firewallAlwaysOn ? "Auto" : "Always On") }

  function splitDescription() {
    if (!confLoaded) return "Loading…"
    if (!splitAvailable) return "Windscribe hasn't written its settings yet"
    if (confError) return "Could not read Windscribe's settings"
    if (!splitOn) return "Keep chosen apps off the VPN"
    var n = splitApps.length
    if (n === 0) return "No apps chosen yet"
    if (splitMode === "Include")
      return n === 1 ? "Only 1 app uses the VPN" : "Only " + n + " apps use the VPN"
    return n === 1 ? "1 app skips the VPN" : n + " apps skip the VPN"
  }

  // Persisted across restarts: last few places connected to, and the fold
  // state of the panel's lists. Location labels only, nothing secret.
  property var recents: []
  property bool countriesExpanded: false
  property bool quickExpanded: true
  property bool staticsExpanded: true
  property bool profilesExpanded: true
  property bool recentsExpanded: true
  property bool stateLoaded: false

  // ── Profiles ────────────────────────────────────────────────────────
  // A named place to connect to, in a colour of the theme: {id, name,
  // color, args, title, subtitle}. `color` is the name of a theme colour
  // ("blue", "magenta"…), never a hex value, so a profile keeps its role
  // across theme switches. `args` is the same argv a recent carries;
  // `title`/`subtitle` describe the place the way Recent would.
  property var profiles: []
  readonly property var profileColors: ["accent", "red", "yellow", "green", "cyan", "blue", "magenta"]
  readonly property int profileNameMax: 32
  // The profile the live connection was started from, or "". Not persisted:
  // after a restart the tunnel is the tunnel, and no profile claims it.
  property string activeProfile: ""
  readonly property var activeProfileEntry: {
    if (activeProfile === "") return null
    for (var i = 0; i < profiles.length; i++) if (profiles[i].id === activeProfile) return profiles[i]
    return null
  }

  // ── Always On ────────────────────────────────────────────────────────
  // One invariant, not a set of triggers: if the switch is on and the
  // tunnel isn't up, bring it up. Signing in, joining a network and a
  // dropped tunnel are all just moments when that stops being true, so
  // the status poll that already runs for the bar icon is the whole
  // mechanism. No second watcher, no second process, nothing to keep in
  // sync.
  //
  // The engine has no always-on of its own. (Its conf has an Autoconnect
  // flag, but that covers the engine's own start-up only, not a tunnel
  // that drops mid-session.)
  property bool autoConnect: false
  // Set when an automatic connect fails, so the next attempt falls back
  // to the engine's best location instead of retrying a place that is
  // full or gone, forever.
  property bool _autoPinFailed: false
  // The last error an automatic connect got, kept out of lastError so the
  // status poll never wipes it: the failures are worth diagnosing long
  // after the flash message has gone. Cleared on a successful connect.
  property string autoLastError: ""
  // Don't retry a failed connect on the very next poll: a connect against
  // a dead network fails slowly and there is no point hammering it.
  readonly property int autoRetryMs: 30000
  property real _autoNextMs: 0
  // True while the in-flight connect was started by the reconciler rather
  // than a click, so only its failures arm the retry delay.
  property bool _autoAttempt: false

  readonly property bool autoReady: installed && signedIn && accountProbed && stateLoaded && autoConnect
  readonly property string stateDir: (Quickshell.env("XDG_STATE_HOME") || (Quickshell.env("HOME") + "/.local/state")) + "/omarchy-windscribe"
  readonly property string statePath: stateDir + "/state.json"

  readonly property string placesScriptPath: Qt.resolvedUrl("places.py").toString().replace(/^file:\/\//, "")
  readonly property string appsScriptPath: Qt.resolvedUrl("apps.py").toString().replace(/^file:\/\//, "")

  property string actionStatus: ""
  property string lastError: ""
  property string pendingLabel: ""
  // Diagnostic only: the argv of the last CLI command this Service built.
  // Exposed through the IPC debug() so the exact command can be inspected.
  property string lastCommand: ""

  // -1 = follow reality; 0/1 = a requested state still catching up.
  property int _desired: -1
  // Set when we asked for the tunnel to go down, so the drop isn't reported
  // as a failure.
  property bool _expectDown: false
  // What the in-flight connect was asked for, recorded to recents on success.
  property var _target: null
  // When the last action finished, and when the in-flight status poll
  // started. A poll that began before the action ended carries pre-action
  // data, so it must not be used to decide whether the action took effect.
  property real _actionEndedMs: 0
  property real _watchStartedMs: 0

  readonly property bool connected: _desired === -1 ? statusConnected : (_desired === 1)
  readonly property bool busy: actionProcess.running || connectProcess.running

  // The engine serializes connects itself, but the bar draws a surface on
  // every monitor and each gets its own copy of this Service. Two of them
  // asking for a connect at the same moment (Always On, most likely)
  // shouldn't become two engine requests, so every CLI call takes one
  // lock file first and the calls queue instead of overlapping. Probes
  // (status, locations) are safe to run side by side and skip the lock.
  readonly property string cliLockPath: (Quickshell.env("XDG_RUNTIME_DIR") || stateDir) + "/omarchy-windscribe-cli.lock"
  readonly property int cliLockBusy: 75
  function lockBusy(exitCode) { return exitCode === cliLockBusy }

  // Read-only probes are quick but a hung engine would otherwise hold the
  // poll slot forever: Quickshell's Process has no kill, so onExited never
  // fires and the widget sits on "Checking…" for good. Thirty seconds is
  // generous (status answers in ~40ms); -k 5 follows up with SIGKILL.
  //
  // The flock wait is short on purpose: the CLI refuses to run two copies
  // of itself at all ("Windscribe CLI is already running"), so every call
  // serializes through our lock, and during a long connect a probe would
  // otherwise queue behind it for a minute. A probe that finds the lock
  // taken exits with cliLockBusy, which reads as "ask again next poll",
  // never as a failure. The optimistic override holds the UI meanwhile.
  readonly property int probeTimeoutSec: 30
  readonly property int probeLockWaitSec: 2
  // A user-requested connect waits it out: it legitimately takes a minute
  // and a half. Five minutes is the safety valve, not the expectation.
  readonly property int actionTimeoutSec: 300

  function probeCommand(args) {
    return ["flock", "-w", String(probeLockWaitSec), "-E", String(cliLockBusy), cliLockPath,
            "timeout", "-k", "5", String(probeTimeoutSec), "windscribe-cli"].concat(args)
  }
  // Something the person asked for waits for the lock however long it takes.
  function actionCommand(args) {
    return ["flock", cliLockPath, "timeout", "-k", "10", String(actionTimeoutSec), "windscribe-cli"].concat(args)
  }
  // An Always On reconnect doesn't wait at all. Every monitor's widget
  // sees the same drop on the same poll; whichever gets the lock
  // reconnects, and the others find the tunnel back up on their next look
  // instead of queueing a second connect behind the first.
  function autoConnectCommand(args) {
    return ["flock", "-n", "-E", String(cliLockBusy), cliLockPath,
            "timeout", "-k", "10", String(actionTimeoutSec), "windscribe-cli"].concat(args)
  }
  // 124 is `timeout` giving up after SIGTERM, 137 after the SIGKILL.
  function probeStalled(exitCode) { return exitCode === 124 || exitCode === 137 }

  // Consecutive status failures. One is a blip and is retried on the next
  // poll. Three in a row means the engine isn't answering, so the panel
  // says so rather than pretending nothing is known.
  property int _statusFailures: 0
  readonly property int statusGiveUpAfter: 3

  readonly property int watchIntervalSec: intSetting("watchIntervalSec", 4, 2, 60)
  readonly property bool notificationsOn: String(setting("notifications", "on")) !== "off"

  // Where the connected tunnel exits, promoted out of the status parse.
  readonly property string displayWhere: currentWhere
  // A nicer join than the CLI's " - " for the hero and notifications.
  readonly property string displayPlace: {
    var w = displayWhere
    if (w === "") return ""
    return w.split(" - ").join(" · ")
  }
  readonly property string displayStatus: {
    if (!installed) return "Not installed"
    if (busy && pendingLabel !== "") return pendingLabel
    if (connected) return "Protected"
    if (statusConnecting) return "Connecting…"
    if (!accountProbed) return "Checking…"
    if (!signedIn) return loginError !== "" ? "Signed out (" + loginError + ")" : "Signed out"
    if (interference && statusConnected) return "Connected (network interference)"
    if (!connected) return "Not protected"
    return "Checking…"
  }

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    if (n < min) n = min
    if (n > max) n = max
    return n
  }

  function refresh() {
    if (!installed) {
      probeInstalled()
      return
    }
    refreshStatus()
  }

  function probeInstalled() {
    if (!whichProcess.running) {
      whichProcess.command = ["which", "windscribe-cli"]
      whichProcess.running = true
    }
  }

  // Omarchy's own installer flow: a floating terminal owns the password
  // prompt, so the shell process never touches privileges. The CLI is a
  // local package (windscribe-cli isn't in the repos or AUR's normal
  // flow for Omarchy's helper), so the row hands the download+install
  // to the terminal rather than pretending it can click through.
  function installCli() {
    if (installed || installing) return
    installing = true
    lastError = ""
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation",
      "echo 'Install the Windscribe CLI: download windscribe-cli_*_amd64.pkg.tar.zst from windscribe.com/download (Linux), then: sudo pacman -U ~/Downloads/windscribe-cli_*_amd64.pkg.tar.zst'"])
    installWatch.restart()
  }

  function refreshStatus() {
    if (!installed) return
    if (statusProcess.running) return
    _watchStartedMs = Date.now()
    statusProcess.command = probeCommand(["status"])
    statusProcess.running = true
  }

  function loadRegions(force) {
    if (!installed || !signedIn) return
    if (regionsLoaded && force !== true) return
    if (regionsProcess.running) return
    regionsProcess.command = probeCommand(["locations"])
    regionsProcess.running = true
  }

  function loadStatics(force) {
    if (!installed || !signedIn) return
    if (staticsLoaded && force !== true) return
    if (staticsProcess.running) return
    staticsProcess.command = probeCommand(["locations", "static"])
    staticsProcess.running = true
  }

  // The bundled city list: one shot, no lock, no engine involvement.
  function loadCities() {
    if (citiesLoaded || citiesProcess.running) return
    citiesProcess.command = ["python3", placesScriptPath, "--cities"]
    citiesProcess.running = true
  }

  function applyStatus(raw) {
    var parsed = Model.parseStatus(raw)
    signedIn = parsed.signedIn
    if (parsed.loginError !== "") loginError = parsed.loginError
    else if (parsed.signedIn) loginError = ""
    accountProbed = true
    statusConnected = parsed.connected
    statusConnecting = parsed.connecting
    statusDisconnecting = parsed.disconnecting
    statusText = parsed.statusText
    var place = Model.splitPlace(parsed.where)
    currentWhere = parsed.where
    currentCity = parsed.connected ? place.city : ""
    currentNickname = parsed.connected ? place.nickname : ""
    checkingConnection = parsed.checking
    interference = parsed.interference
    protocolLine = parsed.protocol
    ipLine = parsed.ip
    firewallState = parsed.firewall
    dataUsage = parsed.dataUsage
    updateAvailable = parsed.update
    fields = parsed.fields
    reconcile()
  }

  // Drop the optimistic override as soon as the world agrees with it, and
  // also once the action that set it has finished, whatever the outcome.
  //
  // That second condition is not belt-and-braces. The override says
  // "ignore reality, this is where we're heading", and releasing it only
  // on agreement lets a disconnect that something else immediately undoes
  // latch the panel at a lie for the rest of the session. Both callers run
  // this against freshly polled state, so releasing on !busy hands the UI
  // back to an observation, never to a stale one.
  //
  // It deliberately does NOT release while reality disagrees during a
  // connect: a poll mid-connect can still report the old state, and
  // releasing on that would flash "Not protected" over a connect that is
  // succeeding.
  function reconcile() {
    if (_desired === -1) return
    var real = statusConnected
    if (real === (_desired === 1)) {
      _desired = -1
      pendingLabel = ""
      return
    }
    // Safety valve only, never part of a normal connect or disconnect: if
    // the world settles on the opposite of what was asked and stays there,
    // release rather than hold the panel at a lie (a disconnect that
    // something else immediately undoes leaves "Not protected" over a
    // live tunnel, and the power button then tries to connect). Three
    // minutes is far longer than even a slow connect needs to settle.
    if (!busy && _actionEndedMs > 0 && Date.now() - _actionEndedMs > 180000) {
      _desired = -1
      pendingLabel = ""
    }
  }

  function watchApply(text) {
    var parsed = Model.parseStatus(text)
    var was = statusConnected
    applyStatus(text)
    // A tunnel we didn't ask to close is the one thing a person must hear
    // about: the icon dimming is not a signal most people read.
    if (was && !parsed.connected && !_expectDown && !actionProcess.running && !connectProcess.running)
      notify("VPN Disconnected 󰻌", "You're no longer protected.", "critical")
    _expectDown = false
    if (was !== parsed.connected) {
      trafficReset()
      if (parsed.connected) discoverDevice()
    }
    autoReconcile()
  }

  // The only argv a recent or profile may carry: a location name for
  // `connect`, the `static` keyword plus an IP or city, or `best`. The
  // state file is plain JSON under $HOME, so it is checked on the way in
  // rather than trusted on the way to `windscribe-cli connect`. Anything
  // that doesn't fit is dropped, not repaired.
  readonly property var connectArg: /^[A-Za-z0-9 ._-]{1,64}$/
  readonly property var staticValue: /^(?:[A-Za-z0-9 ._-]{1,64}|(\d{1,3}\.){3}\d{1,3})$/

  function cleanArgs(args) {
    var src = (args && typeof args.length === "number") ? args : []
    var out = []
    for (var i = 0; i < src.length && i < 5; i++) {
      var a = String(src[i])
      if (a === "static") {
        if (i + 1 >= src.length || !staticValue.test(String(src[i + 1]))) return null
        out.push("static")
        out.push(String(src[i + 1]))
        i += 1
        continue
      }
      if (!connectArg.test(a)) return null
      out.push(a)
    }
    return out
  }

  function cleanRecent(r) {
    if (!r || typeof r !== "object" || typeof r.key !== "string" || !Array.isArray(r.args)) return null
    var args = cleanArgs(r.args)
    if (args === null) return null
    return { key: r.key, title: String(r.title || ""), subtitle: String(r.subtitle || ""), args: args }
  }

  // Same rules as a recent for the argv, plus a name that fits on a row
  // and a colour that is one of the theme's names. Anything else is dropped.
  function cleanProfile(p) {
    if (!p || typeof p !== "object" || typeof p.id !== "string" || !/^[A-Za-z0-9]{1,24}$/.test(p.id)) return null
    var place = cleanRecent({ key: p.id, title: p.title, subtitle: p.subtitle, args: p.args })
    if (!place) return null
    var name = String(p.name || "").replace(/\s+/g, " ").trim().slice(0, profileNameMax)
    if (name === "") return null
    var color = profileColors.indexOf(p.color) !== -1 ? p.color : "accent"
    return { id: p.id, name: name, color: color, args: place.args, title: place.title, subtitle: place.subtitle }
  }

  function applyState(text) {
    // An empty re-read is as unusable as an unparseable one, see the catch.
    if (stateLoaded && String(text || "").trim() === "") return
    try {
      var s = JSON.parse(String(text || "{}"))
      recents = Array.isArray(s.recents) ? s.recents.slice(0, 3).map(cleanRecent).filter(Boolean) : []
      profiles = Array.isArray(s.profiles) ? s.profiles.slice(0, 24).map(cleanProfile).filter(Boolean) : []
      autoConnect = s.autoConnect === true
      countriesExpanded = s.countriesExpanded === true
      quickExpanded = s.quickExpanded !== false
      staticsExpanded = s.staticsExpanded !== false
      profilesExpanded = s.profilesExpanded !== false
      recentsExpanded = s.recentsExpanded !== false
    } catch (e) {
      // A re-read that can't be parsed changes nothing: saving defaults
      // over it from here would wipe every screen's profiles.
      if (stateLoaded) return
      recents = []
      profiles = []
      autoConnect = false
      countriesExpanded = false
      quickExpanded = true
      staticsExpanded = true
      profilesExpanded = true
      recentsExpanded = true
    }
    stateLoaded = true
  }

  function saveState() {
    stateFile.setText(JSON.stringify({
      recents: recents,
      profiles: profiles,
      autoConnect: autoConnect,
      countriesExpanded: countriesExpanded,
      quickExpanded: quickExpanded,
      staticsExpanded: staticsExpanded,
      profilesExpanded: profilesExpanded,
      recentsExpanded: recentsExpanded
    }))
  }

  function setCountriesExpanded(on) {
    if (countriesExpanded === on) return
    countriesExpanded = on
    saveState()
  }

  function setQuickExpanded(on) {
    if (quickExpanded === on) return
    quickExpanded = on
    saveState()
  }

  function setStaticsExpanded(on) {
    if (staticsExpanded === on) return
    staticsExpanded = on
    saveState()
  }

  function setProfilesExpanded(on) {
    if (profilesExpanded === on) return
    profilesExpanded = on
    saveState()
  }

  function setRecentsExpanded(on) {
    if (recentsExpanded === on) return
    recentsExpanded = on
    saveState()
  }

  // ── Always On ───────────────────────────────────────────────────────

  function toggleAutoConnect() {
    autoConnect = !autoConnect
    _autoNextMs = 0
    saveState()
    autoReconcile()
  }

  // Run on every status poll, and checked against the world as it is
  // right now.
  //
  // Back to the top of Recent, which is the place we were last actually
  // on, however we got there. If that one fails it is skipped next time
  // round, so a place that is full or gone can't stall this forever.
  function autoReconcile() {
    if (!autoReady) return
    if (connected || statusConnecting || busy) return
    if (_autoNextMs > 0 && Date.now() < _autoNextMs) return
    var t = recents.length > 0 && Array.isArray(recents[0].args) ? recents[0] : null
    if (t && !_autoPinFailed) connectTo(t.args, "Reconnecting to " + t.title + "…", t, true)
    else connectTo([], "Reconnecting to last used…", null, true)
  }

  function recordRecent(target) {
    if (!target || !target.key) return
    var next = [target]
    for (var i = 0; i < recents.length && next.length < 3; i++) {
      if (recents[i] && recents[i].key !== target.key) next.push(recents[i])
    }
    recents = next
    saveState()
  }

  // target: {key, title, subtitle, args} recorded to recents on success, or
  // null for quick actions that are already one click away.
  // Which Quick Connect row the current connection was asked with, or ""
  // for a named place: that row's glyph fills in while you are on it. Not
  // persisted; after a restart the header shows the place itself.
  property string quickRequested: ""

  function connectTo(args, label, target, auto) {
    if (!installed || !signedIn || busy) return
    // Any connect that isn't a profile click ends the profile's claim.
    activeProfile = ""
    var a = cleanArgs(args) || []
    quickRequested = a.length === 1 && a[0] === "best" ? "best" : (a.length === 0 ? "last" : "")
    _autoAttempt = auto === true
    _desired = 1
    _expectDown = false
    _target = target
    pendingLabel = label || "Connecting…"
    actionStatus = pendingLabel
    lastError = ""
    connectProcess.command = (_autoAttempt ? autoConnectCommand : actionCommand)(["connect"].concat(a))
    root.lastCommand = JSON.stringify(connectProcess.command)
    connectProcess.running = true
  }

  function connectBest() { connectTo(["best"], "Connecting to best location…", null) }
  function connectLast() { connectTo([], "Connecting to last used…", null) }

  function connectStatic(entry) {
    if (!entry) return
    // The CLI takes the static by city or by IP; the IP is the more
    // precise of the two, exactly as `locations static` prints it.
    connectTo(["static", entry.ip], "Connecting to " + entry.city + " (static)…",
              { key: "static:" + entry.ip, title: entry.city, subtitle: "Static IP · " + entry.ip, args: ["static", entry.ip] })
  }

  function connectCity(city, regionName) {
    var c = String(city || "").trim()
    if (!connectArg.test(c)) return
    var title = c
    var subtitle = String(regionName || "").trim()
    connectTo([c], "Connecting to " + title + "…",
              { key: "city:" + c.toLowerCase(), title: title, subtitle: subtitle, args: [c] })
  }

  function connectRegion(name) {
    var r = String(name || "").trim()
    if (!connectArg.test(r)) return
    connectTo([r], "Connecting to " + r + "…",
              { key: "region:" + r.toLowerCase(), title: r, subtitle: "Fastest datacenter in region", args: [r] })
  }

  function connectRecent(index) {
    var r = recents[index]
    if (!r || !Array.isArray(r.args)) return
    connectTo(r.args, "Connecting to " + r.title + "…", r)
  }

  // A profile connects like the row it was made from: a city or region
  // profile records that place to Recent, a best/last/static-only one
  // records wherever the engine landed, exactly as the quick rows do.
  function connectProfile(id) {
    var p = null
    for (var i = 0; i < profiles.length; i++) if (profiles[i].id === id) p = profiles[i]
    if (!p || busy) return
    var target = profileNamesPlace(p.args)
      ? { key: "args:" + p.args.join(" "), title: p.title, subtitle: p.subtitle, args: p.args }
      : null
    connectTo(p.args, "Connecting to " + p.name + "…", target)
    if (connectProcess.running) activeProfile = p.id
  }

  function profileNamesPlace(args) {
    // "static X" and a bare location name a place; best/last don't.
    for (var i = 0; i < args.length; i++) {
      if (args[i] === "static") return true
      if (args[i] === "best") continue
      if (String(args[i]).charAt(0) !== "-") return true
    }
    return false
  }

  // Build the args and the place description from the editor's choices.
  // `where` is "" (last used), "best", "static:<ip or city>",
  // "region:<name>" or "city:<name>".
  function buildProfile(id, name, color, where, placeTitle, placeSubtitle) {
    var args = []
    var title = "Last used"
    var subtitle = "Wherever you connected to last"
    var w = String(where || "")
    if (w === "best") { args = ["best"]; title = "Best location"; subtitle = "The engine's pick for your network" }
    else if (w.indexOf("static:") === 0) {
      var s = w.slice(7)
      if (!staticValue.test(s)) return null
      args = ["static", s]
      title = placeTitle || s
      subtitle = placeSubtitle || "Static IP"
    } else if (w.indexOf("region:") === 0) {
      var r = w.slice(7)
      if (!connectArg.test(r)) return null
      args = [r]; title = r; subtitle = "Fastest datacenter in region"
    } else if (w.indexOf("city:") === 0) {
      var c = w.slice(5)
      if (!connectArg.test(c)) return null
      args = [c]; title = c; subtitle = "Random datacenter in city"
    }
    return cleanProfile({ id: id, name: name, color: color, args: args, title: title, subtitle: subtitle })
  }

  // Upsert by id; a new profile gets a fresh id and goes last.
  function saveProfile(p) {
    var clean = cleanProfile(p)
    if (!clean) return false
    var next = profiles.slice()
    var found = false
    for (var i = 0; i < next.length; i++) if (next[i].id === clean.id) { next[i] = clean; found = true }
    if (!found) next.push(clean)
    profiles = next
    saveState()
    return true
  }

  function deleteProfile(id) {
    profiles = profiles.filter(function(p) { return p.id !== id })
    if (activeProfile === id) activeProfile = ""
    saveState()
  }

  function newProfileId() {
    return "p" + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36)
  }

  function regionName(name) {
    var n = String(name || "")
    for (var i = 0; i < regions.length; i++) if (regions[i].name === n) return regions[i]
    return null
  }

  function toggle() {
    if (!installed || busy) return
    // A live tunnel is proof of a session, so never send an
    // already-signed-in user to a sign-in prompt just because the status
    // probe is stale or briefly failed.
    if (!signedIn && !connected) {
      if (accountProbed) signIn("")
      else refreshStatus()
      return
    }
    if (connected) disconnect()
    else connectBest()
  }

  function disconnect() {
    if (!installed || busy) return
    _desired = 0
    _expectDown = true
    trafficReset()
    pendingLabel = "Disconnecting…"
    actionStatus = ""
    lastError = ""
    actionProcess.command = actionCommand(["disconnect"])
    actionProcess.running = true
  }

  // The firewall toggle: `firewall on|off` acts on the engine right now.
  // With the mode set to "Always On" the engine refuses `off`, and the
  // row is disabled rather than offering a click that can only fail.
  function toggleFirewall() {
    if (!installed || busy) return
    if (firewallAlwaysOn) return
    var on = String(firewallState).toLowerCase() !== "on"
    pendingLabel = on ? "Turning firewall on…" : "Turning firewall off…"
    actionStatus = ""
    lastError = ""
    actionProcess.command = actionCommand(["firewall", on ? "on" : "off"])
    actionProcess.running = true
  }

  // A different IP on the same server. Exit 2 while a rotation is still
  // in progress; that reads as "try again", not as a failure.
  function rotateIp() {
    if (!installed || !connected || rotateProcess.running) return
    actionStatus = "Rotating IP…"
    actionStatusTimer.restart()
    rotateProcess.command = actionCommand(["ip", "rotate"])
    rotateProcess.running = true
  }

  // Sign-in is interactive (password, then a 2FA code if the account uses
  // one), so it has to happen in a real terminal: the CLI only accepts
  // them from a tty. The username can be typed in the panel; with none
  // given the terminal asks for it.
  function signIn(username) {
    var u = String(username || "").trim()
    var lock = Model.shellQuote(cliLockPath)
    var cmd
    if (u === "") {
      cmd = "read -rp 'Windscribe username: ' u && flock " + lock + " windscribe-cli login \"$u\""
    } else if (Model.validUsername(u)) {
      cmd = "flock " + lock + " windscribe-cli login " + Model.shellQuote(u)
    } else {
      lastError = "Usernames can only contain letters, numbers and . _ + @ -"
      return false
    }
    lastError = ""
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", cmd])
    return true
  }

  function signOut() {
    if (!installed || busy) return
    _desired = 0
    _expectDown = true
    trafficReset()
    pendingLabel = "Signing out…"
    actionProcess.command = actionCommand(["logout"])
    actionProcess.running = true
  }

  function trafficReset() {
    rxHistory = []; txHistory = []
    rxRate = 0; txRate = 0
    sessionRx = 0; sessionTx = 0
    uptimeSec = 0
    _lastRx = -1; _lastTx = -1; _lastSampleMs = 0
    linkDevice = ""
    _linkUpMs = Date.now()
  }

  // The engine names its tunnel utun<NNN>, and there is no other way to
  // learn the number. `ls /sys/class/net` and pick the utun device; with
  // none there yet, the next sample tick tries again.
  function discoverDevice() {
    if (!statusConnected || deviceProcess.running) return
    deviceProcess.command = ["ls", "-1", "/sys/class/net"]
    deviceProcess.running = true
  }

  function deviceApply(text) {
    var lines = String(text || "").split("\n")
    for (var i = 0; i < lines.length; i++) {
      var name = lines[i].trim()
      if (/^utun[0-9]+$/.test(name)) { linkDevice = name; return }
    }
  }

  // sysfs files report a size of 0, so FileView reads them as empty; `cat`
  // reads until EOF and costs about a millisecond once a second.
  function trafficSample() {
    if (trafficProcess.running) return
    if (linkDevice === "") { discoverDevice(); return }
    var base = "/sys/class/net/" + linkDevice + "/statistics/"
    trafficProcess.command = ["cat", base + "rx_bytes", base + "tx_bytes"]
    trafficProcess.running = true
  }

  function trafficApply(text) {
    var parts = String(text || "").trim().split(/\s+/)
    var rx = parseFloat(parts[0]), tx = parseFloat(parts[1])
    if (!isFinite(rx) || !isFinite(tx)) return
    var now = Date.now()
    if (_lastRx >= 0 && _lastSampleMs > 0) {
      var dt = Math.max(0.25, (now - _lastSampleMs) / 1000)
      var drx = Math.max(0, rx - _lastRx), dtx = Math.max(0, tx - _lastTx)
      rxRate = drx / dt; txRate = dtx / dt
      sessionRx += drx; sessionTx += dtx
      var h = rxHistory.slice(); h.push(rxRate); if (h.length > trafficSamples) h.shift(); rxHistory = h
      var g = txHistory.slice(); g.push(txRate); if (g.length > trafficSamples) g.shift(); txHistory = g
    }
    _lastRx = rx; _lastTx = tx; _lastSampleMs = now
    uptimeSec = _linkUpMs > 0 ? Math.floor((now - _linkUpMs) / 1000) : 0
  }

  // Which city the connected tunnel exits from. Runs whenever the place
  // changes; a lookup arriving mid-run is queued, not dropped.
  function locatePlace(city) {
    var c = String(city || "").trim()
    if (c === "") { currentPlace = null; _locatePending = ""; return }
    if (currentPlace && currentPlace.city === c) return
    if (locateProcess.running) { _locatePending = c; return }
    locateProcess.command = ["python3", placesScriptPath, "--locate", c]
    locateProcess.running = true
  }

  onCurrentCityChanged: locatePlace(currentCity)
  onConnectedChanged: if (!connected) { activeProfile = ""; currentPlace = null; trafficReset() }

  // The plugin's own mark for the toast, written to the state dir in the
  // theme's accent colour so it wears every Omarchy theme like the widget
  // does, and rewritten whenever the theme changes. A themed-icon name was
  // the wrong tool here: most icon themes don't carry `network-vpn`, and
  // the shell drew Qt's missing-texture checkerboard in its place.
  readonly property string notificationIconPath: stateDir + "/notification-icon.svg"
  // The Windscribe W, from the wordmark's own path, in the 16.49x20
  // source viewBox.
  readonly property string windscribeMarkPath: "M3.32021 16.0805L8.24604 10.5545L13.1719 16.081V5.16809H16.4916V19.8788H12.597L12.5249 19.7973L8.24556 14.9536L3.96673 19.7973L3.8946 19.8788H0V5.16809H3.32021V16.0805Z"

  function writeNotificationIcon() {
    // Qt prints an opaque colour as #rrggbb and a translucent one as
    // #aarrggbb; SVG wants the former, so drop the alpha if present.
    var hex = String(Commons.Color.accent)
    if (hex.length === 9) hex = "#" + hex.slice(3)
    notificationIconFile.setText('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16.49 20" width="20" height="24">'
                                 + '<path fill="' + hex + '" d="' + windscribeMarkPath + '"/></svg>\n')
  }

  function notify(summary, body, urgency) {
    if (!notificationsOn) return
    // The panel already shows the state change in its header and map, so a
    // toast on top of it is noise. Only notify when the panel isn't open.
    if (panelOpen) return
    // Call org.freedesktop.Notifications.Notify directly instead of going
    // through notify-send or the omarchy-notification-send wrapper: one
    // process, no argv reparsing of the summary/body, and the omarchy-glyph
    // hint gives the shell a Nerd Font shield-lock to draw if the SVG isn't
    // there yet (first launch) or fails to load. Signature: app_name,
    // replaces_id, app_icon, summary, body, actions, hints, expire_timeout.
    var level = urgency === "critical" ? "2" : (urgency === "low" ? "0" : "1")
    Quickshell.execDetached(["busctl", "--user", "--", "call",
                             "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                             "org.freedesktop.Notifications", "Notify", "susssasa{sv}i",
                             "Windscribe", "0", notificationIconPath, summary, Model.escapeMarkup(body),
                             "0",
                             "2", "urgency", "y", level, "omarchy-glyph", "s", "\udb81\udc98",
                             "-1"])
  }

  Component.onCompleted: {
    // Owner-only, and fixed up on existing installs too: the file holds
    // where you've been connecting, which is nobody else's business on a
    // shared box.
    Quickshell.execDetached(["install", "-d", "-m", "700", stateDir])
    refresh()
  }

  // The state dir is created by a detached process, so the icon is written
  // a moment later rather than racing it on a first launch.
  Timer {
    interval: 1000
    running: true
    repeat: false
    onTriggered: root.writeNotificationIcon()
  }

  Connections {
    target: Commons.Color
    function onAccentChanged() { root.writeNotificationIcon() }
  }

  onPanelOpenChanged: if (panelOpen) {
    refreshStatus()
    loadRegions(false)
    loadStatics(false)
  }

  onSignedInChanged: {
    if (signedIn) {
      loadRegions(true)
      loadStatics(true)
      loadCities()
    } else {
      regions = []
      regionsLoaded = false
      statics = []
      staticsLoaded = false
      confLoaded = false
      confConnection = null
    }
  }

  Process {
    id: trafficProcess
    running: false
    command: []
    stdout: StdioCollector { id: trafficStdout; waitForEnd: true }
    onExited: function(exitCode) { if (exitCode === 0) root.trafficApply(String(trafficStdout.text || "")) }
  }

  Timer {
    id: trafficTimer
    interval: 1000
    repeat: true
    running: root.panelOpen && root.connected && root.statusConnected
    triggeredOnStart: true
    onTriggered: root.trafficSample()
  }

  FileView {
    id: notificationIconFile
    path: root.notificationIconPath
    printErrors: false
    atomicWrites: true
  }

  // Watched, because every monitor's widget holds its own copy: without the
  // re-read, a profile saved on one screen is written over the next time
  // another screen saves anything, even just a fold.
  FileView {
    id: stateFile
    path: root.statePath
    printErrors: false
    watchChanges: true
    atomicWrites: true
    onFileChanged: reload()
    onLoaded: root.applyState(text())
    // Only a first read may fall back to defaults. After that, a file that's
    // gone keeps what's in memory, and the next save puts it back.
    onLoadFailed: if (!root.stateLoaded) root.applyState("{}")
  }

  // Windscribe's file, not ours. Watched rather than polled, because the
  // engine rewrites it from its own GUI and the panel would otherwise
  // show a stale copy of a file that just changed underneath it.
  FileView {
    id: confFile
    path: root.confPath
    printErrors: false
    watchChanges: true
    atomicWrites: true
    onLoaded: {
      root.confMissing = false
      root.confError = false
      root.confConnection = Model.parseConfSection(text(), "Connection")
      root.confLoaded = true
    }
    onFileChanged: reload()
    // No file yet means Windscribe hasn't run here; the split row says so
    // rather than offering a switch that would have to create one.
    onLoadFailed: { root.confMissing = true; root.confConnection = null; root.confLoaded = true }
    onSaveFailed: root.confError = true
  }

  Timer {
    id: watchTimer
    interval: root.watchIntervalSec * 1000
    repeat: true
    running: root.installed
    triggeredOnStart: true
    onTriggered: root.refreshStatus()
  }

  // A connect or disconnect settles in the engine's own time; pull fresh
  // state shortly after the action ends, so the override is released
  // against reality as soon as possible rather than waiting on the poll.
  Timer {
    id: delayedRefresh
    interval: 1500
    repeat: true
    running: root.busy
    onTriggered: if (!root.busy) { root.refreshStatus(); stop() }
  }

  Timer {
    id: actionStatusTimer
    interval: 6000
    repeat: false
    onTriggered: root.actionStatus = ""
  }

  // Sign-in happens out of process in a terminal; the next poll sees the
  // new state, so no watcher is needed beyond the poll itself. The CLI
  // answers in ~40ms, which is the whole reason one poll can own this.

  // Same idea for the package install: up to five minutes for pacman.
  Timer {
    id: installWatch
    interval: 3000
    repeat: true
    property int ticks: 0
    onRunningChanged: if (running) ticks = 0
    onTriggered: {
      ticks += 1
      root.probeInstalled()
      if (root.installed || ticks > 100) {
        stop()
        root.installing = false
      }
    }
  }

  Process {
    id: whichProcess
    running: false
    command: []
    onExited: function(exitCode) {
      var was = root.installed
      root.installed = exitCode === 0
      if (root.installed) {
        if (!was) {
          root.installing = false
          root.lastError = ""
          root.statusText = "Checking…"
        }
        root.refresh()
        root.loadCities()
      } else if (!root.installing) {
        root.statusText = "Windscribe CLI not installed"
      }
    }
  }

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: StdioCollector { id: statusStdout; waitForEnd: true }
    stderr: StdioCollector { id: statusStderr; waitForEnd: true }
    onExited: function(exitCode, exitStatus) {
      var out = String(statusStdout.text || "")
      var err = String(statusStderr.text || "")
      // The lock was taken, most likely by a connect from this or another
      // monitor's widget: "ask again next poll", never a failure. The
      // optimistic override holds the UI meanwhile.
      if (root.lockBusy(exitCode)) return
      // Someone ran windscribe-cli in a terminal and the CLI's own global
      // lock rejected our probe. Transient: keep what we know, retry next
      // poll, never count it toward giving up.
      if (exitCode !== 0 && /already running/i.test(err)) return
      var crashed = exitStatus !== 0 || exitCode > 128 || root.probeStalled(exitCode)
      if (exitCode === 0 || (crashed && Model.statusComplete(out))) {
        root._statusFailures = 0
        root.watchApply(out)
        root.lastError = ""
        return
      }
      root._statusFailures += 1
      if (root._statusFailures >= root.statusGiveUpAfter) {
        root.accountProbed = true
        root.lastError = root.probeStalled(exitCode)
          ? "Windscribe isn't answering. Is the windscribe service running?"
          : Model.elide(err || "windscribe-cli status failed")
      }
    }
  }

  Process {
    id: regionsProcess
    running: false
    command: []
    stdout: StdioCollector { id: regionsStdout; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0) return
      var grouped = Model.groupLocations(Model.parseLocations(String(regionsStdout.text || "")))
      var map = ({})
      for (var i = 0; i < grouped.length; i++) {
        var cities = grouped[i].cities
        for (var j = 0; j < cities.length; j++) map[cities[j].name.toLowerCase()] = grouped[i].name
      }
      root.cityRegion = map
      root.regions = grouped
      root.regionsLoaded = grouped.length > 0
    }
  }

  Process {
    id: staticsProcess
    running: false
    command: []
    stdout: StdioCollector { id: staticsStdout; waitForEnd: true }
    onExited: function(exitCode) {
      root.statics = exitCode === 0 ? Model.parseStatics(String(staticsStdout.text || "")) : []
      root.staticsLoaded = true
    }
  }

  Process {
    id: citiesProcess
    running: false
    command: []
    stdout: StdioCollector { id: citiesStdout; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0) return
      try {
        var list = JSON.parse(String(citiesStdout.text || "[]"))
        root.cities = Array.isArray(list) ? list : []
        root.citiesLoaded = root.cities.length > 0
      } catch (e) {
        root.cities = []
      }
    }
  }

  Process {
    id: locateProcess
    running: false
    command: []
    stdout: StdioCollector { id: locateStdout; waitForEnd: true }
    onExited: function(exitCode) {
      try {
        var place = exitCode === 0 ? JSON.parse(String(locateStdout.text || "{}")) : {}
        root.currentPlace = place && place.lat !== undefined && place.lat !== null ? place : null
      } catch (e) {
        root.currentPlace = null
      }
      if (root._locatePending !== "") {
        var next = root._locatePending
        root._locatePending = ""
        root.locatePlace(next)
      }
    }
  }

  Process {
    id: deviceProcess
    running: false
    command: []
    stdout: StdioCollector { id: deviceStdout; waitForEnd: true }
    onExited: function(exitCode) { if (exitCode === 0) root.deviceApply(String(deviceStdout.text || "")) }
  }

  Process {
    id: rotateProcess
    running: false
    command: []
    stdout: StdioCollector { id: rotateStdout; waitForEnd: true }
    stderr: StdioCollector { id: rotateStderr; waitForEnd: true }
    onExited: function(exitCode) {
      // 2 while a rotation is still in progress: not a failure, just
      // nothing to report yet.
      if (exitCode === 0) {
        root.actionStatus = "IP rotated"
        root.actionStatusTimer.restart()
        root.refreshStatus()
      } else if (exitCode !== 2) {
        root.lastError = Model.elide(String(rotateStderr.text || "") || "IP rotation failed")
      }
    }
  }

  Process {
    id: connectProcess
    running: false
    command: []
    stdout: StdioCollector { id: connectStdout; waitForEnd: true }
    stderr: StdioCollector { id: connectStderr; waitForEnd: true }
    onExited: function(exitCode) {
      root._actionEndedMs = Date.now()
      var out = String(connectStdout.text || "")
      var err = String(connectStderr.text || "")
      var target = root._target
      var wasAuto = root._autoAttempt
      root._target = null
      root._autoAttempt = false
      root.pendingLabel = ""
      // An Always On reconnect that found the CLI taken, most likely by the
      // same reconnect from another monitor. Not a failure: nothing is
      // marked, nothing is said, and the next poll looks again.
      if (wasAuto && root.lockBusy(exitCode)) {
        root._desired = -1
        root.actionStatus = ""
        // Arm the retry delay here too: without it the next poll would
        // fire another attempt immediately, and every monitor's widget
        // hammering the lock is exactly what the delay is for.
        root._autoNextMs = Date.now() + root.autoRetryMs
        return
      }
      root._autoNextMs = wasAuto && exitCode !== 0 ? Date.now() + root.autoRetryMs : 0
      if (exitCode === 0) root._autoPinFailed = false
      else if (wasAuto) root._autoPinFailed = true
      if (exitCode !== 0) {
        root._desired = -1
        root.activeProfile = ""
        var text = err || out || "Connect failed"
        if (wasAuto) root.autoLastError = Model.elide(text)
        root.lastError = Model.elide(text)
        root.actionStatus = root.lastError
        actionStatusTimer.restart()
      } else {
        root.lastError = ""
        if (wasAuto) root.autoLastError = ""
        // The confirmation line, which is not always the last one: a
        // connect prints "Connecting", "Connected (checking)", then
        // "Connected", and the final one is what names the place.
        var line = Model.elide(Model.connectedLine(out) || out.split("\n")[0] || "", 90)
        root.actionStatus = line
        actionStatusTimer.restart()
        // Best and Last-used arrive here with no target because you
        // didn't name a destination, but you still ended up somewhere.
        // Recover it from the CLI's own confirmation line so Recent is a
        // record of where you've been, not only of what you clicked.
        if (!target) {
          var landed = Model.parseConnected(out)
          if (landed && landed.city !== "") target = {
            key: "city:" + landed.city.toLowerCase(),
            title: landed.city,
            subtitle: landed.nickname,
            args: [landed.city]
          }
        }
        root.recordRecent(target)
        root.notify("VPN Connected \udb82\udd9d", root.displayPlace || "Connected", "normal")
      }
      // Look at the engine now rather than waiting for the poll, so the
      // override is released against fresh data as soon as possible.
      root.refreshStatus()
    }
  }

  Process {
    id: actionProcess
    running: false
    command: []
    stdout: StdioCollector { id: actionStdout; waitForEnd: true }
    stderr: StdioCollector { id: actionStderr; waitForEnd: true }
    onExited: function(exitCode) {
      root._actionEndedMs = Date.now()
      var out = String(actionStdout.text || "")
      var err = String(actionStderr.text || "")
      root.pendingLabel = ""
      if (exitCode !== 0) {
        root._desired = -1
        root._expectDown = false
        root.lastError = Model.elide(err || out || "Command failed")
        root.actionStatus = root.lastError
        actionStatusTimer.restart()
      } else {
        root.lastError = ""
        root.actionStatus = ""
      }
      if (root.signedIn !== true) {
        // A logout ends the session; drop everything the session owned.
        root.regions = []
        root.regionsLoaded = false
        root.statics = []
        root.staticsLoaded = false
      }
      root.refreshStatus()
    }
  }
}
