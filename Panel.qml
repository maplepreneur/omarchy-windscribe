import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "io.github.maplepreneur.omarchy-windscribe"
  ipcTarget: "io.github.maplepreneur.omarchy-windscribe"
  manageIpc: false

  // One keyboard cursor walks every section top to bottom. Each section has a
  // name, a row count, and its own index property; sectionList() says which
  // ones exist right now.
  property string focusSection: "header"
  // Which of the two tabs is showing.
  property string tab: "connections"
  readonly property var tabs: [
    { key: "connections", label: "Connections" },
    { key: "protection", label: "Protection" }
  ]
  property int quickIndex: 0
  property int staticIndex: 0
  property int protectionIndex: 0

  // Keyboard and mouse were fighting over the cursor. Moving with j or k
  // scrolls the list, which slides a different row under a pointer that never
  // moved; that row's hover then drags the cursor back to it and the view
  // snaps to follow, so the list walks itself backwards and you key through
  // the same rows again. Hover only counts once the pointer has actually
  // moved. The shell's PointerMoveGate does this per row from
  // onPositionChanged, which the shared Toggle and MultiSelect don't expose,
  // so it's done once here for the whole panel instead.
  property bool pointerMoved: false

  function setCursorFromHover(section, index) {
    if (!pointerMoved) return
    setCursor(section, index)
  }

  // The Protection section grows by two rows when split tunneling is on,
  // so its cursor length and the position of the sign-out row are computed
  // rather than counted by hand. The caption under the app list carries no
  // cursor of its own.
  readonly property bool splitDetailVisible: vpn.splitOn
  // The service and the dialogs are private children of this panel; these are
  // what a harness needs to check the firewall actually moved, and that it
  // asked first.
  readonly property string vpnFirewall: vpn.firewallState
  readonly property int protectionCount: splitDetailVisible ? 8 : 6
  readonly property int lanIndex: splitDetailVisible ? 6 : 4
  readonly property int signOutIndex: protectionCount - 1

  // App paths Windscribe's file already holds that the scan no longer finds,
  // kept as options so an app removed from the system can still be unticked.
  readonly property var splitStaleApps: {
    var chosen = vpn.splitApps
    var out = []
    for (var i = 0; i < chosen.length; i++) {
      var path = String(chosen[i])
      var name = path.split("/").pop()
      out.push({ value: path, label: name, description: path })
    }
    return out
  }

  onSplitDetailVisibleChanged: {
    if (protectionIndex >= protectionCount) protectionIndex = protectionCount - 1
  }
  property int profileIndex: 0
  property int editorIndex: 0
  property int recentIndex: 0
  property int regionIndex: 0
  property int cityIndex: 0
  property bool cursorActive: false
  property string filterQuery: ""
  // The city that was clicked on the map: its row is scrolled into view and
  // pulses until the next click anywhere. {region, city} or null.
  property var highlight: null
  // Sign out is destructive (it disconnects too), so it takes two clicks
  // within five seconds.
  property bool signOutArmed: false

  // Drilled into one region's city list rather than the region list.
  readonly property var drilledRegion: vpn.regionName(drilledName)
  property string drilledName: ""
  readonly property bool drilled: drilledName !== ""
  // Row 0 inside a drill is "Fastest in <region>"; real cities follow it.
  readonly property int cityRowCount: drilled ? (drilledRegion ? drilledRegion.cities.length + 1 : 1) : 0

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property color iconColor: vpn.connected ? foreground : dim
  readonly property color barIconColor: vpn.connected ? barForeground : Qt.darker(barForeground, 1.55)

  // Location picking is a Pro feature: a free account's CLI refuses every
  // connect that names a place. The account is Pro when the status shows an
  // Unlimited data allowance, so the tags wear the plan the CLI enforces.
  readonly property bool proAccount: vpn.dataUsage.indexOf("Unlimited") !== -1

  // ── Theme palette ───────────────────────────────────────────────────────
  // The shell's Color only carries accent, foreground, background, urgent and
  // muted. A profile wants the rest of the theme's names (Tokyo Night's pinks
  // and purples, Jade's greens), so colors.toml is read here too, with the
  // same line rule the shell uses. Profiles store the name, never the hex,
  // so a theme switch recolours every row on its own.
  property var themePalette: ({})

  function themeColor(name) {
    var n = String(name || "")
    if (n === "" || n === "accent") return Commons.Color.accent
    var v = themePalette[n]
    return v ? v : Commons.Color.accent
  }

  function loadPalette(raw) {
    var lines = String(raw || "").split("\n")
    var out = {}
    for (var i = 0; i < lines.length; i++) {
      var m = lines[i].match(/^\s*([A-Za-z0-9_-]+)\s*=\s*["']?(#[0-9A-Fa-f]{6})/)
      if (m) out[m[1]] = m[2]
    }
    themePalette = out
  }

  FileView {
    id: paletteFile
    path: Commons.Color.currentThemePath + "/colors.toml"
    watchChanges: false
    printErrors: false
    onLoaded: root.loadPalette(text())
    onLoadFailed: root.themePalette = {}
  }

  // A theme switch reaches the shell as a new accent or foreground; the
  // symlink under it has changed by then, so re-read the palette too.
  Connections {
    target: Commons.Color
    function onAccentChanged() { paletteFile.reload() }
    function onForegroundChanged() { paletteFile.reload() }
    function onBackgroundChanged() { paletteFile.reload() }
  }

  // ── Profile editor ──────────────────────────────────────────────────────
  // The draft being edited, or null. {id, isNew, color, where, placeOption};
  // the name lives in nameField until Save. `where` is "", "best",
  // "static:<ip>", "region:<name>" or "city:<name>"; `placeOption` is the
  // dropdown row for a place the option list can't rebuild (a recent from a
  // place the location list no longer carries).
  property var draft: null
  // The buttons are one row of the walk: h and l pick Save, Cancel or
  // Delete along it, the way they pick a colour swatch.
  readonly property var editorRows: draft === null ? [] : ["name", "color", "where", "buttons"]
  readonly property string editorRow: draft !== null && editorIndex < editorRows.length ? editorRows[editorIndex] : ""
  readonly property var editorButtons: draft === null ? [] : (draft.isNew ? ["save", "cancel"] : ["save", "cancel", "delete"])
  property int editorButtonIndex: 0
  readonly property string editorButton: editorRow === "buttons" && editorButtonIndex < editorButtons.length
                                         ? editorButtons[editorButtonIndex] : ""

  readonly property var whereOptions: {
    var list = [
      { value: "", label: "Last used", description: "Wherever you connected to last" },
      { value: "best", label: "Best location", description: "The engine's pick for your network" }
    ]
    for (var i = 0; i < vpn.statics.length; i++) {
      var s = vpn.statics[i]
      list.push({ value: "static:" + s.ip, label: s.city + " (static)", description: "Static IP · " + s.ip })
    }
    for (var r = 0; r < vpn.regions.length; r++) {
      var region = vpn.regions[r]
      list.push({ value: "region:" + region.name, label: region.name, description: "Fastest datacenter in the region" })
    }
    for (var c = 0; c < vpn.regions.length; c++) {
      var cities = vpn.regions[c].cities
      for (var j = 0; j < cities.length; j++) {
        list.push({ value: "city:" + cities[j].name, label: cities[j].name, description: vpn.regions[c].name + " · random datacenter in city" })
      }
    }
    if (draft !== null && draft.placeOption) list.push(draft.placeOption)
    return list
  }

  function whereOf(args) {
    var a = Array.isArray(args) ? args : []
    for (var i = 0; i < a.length; i++) {
      if (a[i] === "best") return "best"
      if (a[i] === "static" && i + 1 < a.length) return "static:" + a[i + 1]
      if (a[i].charAt(0) !== "-") {
        var name = String(a[i])
        var known = vpn.cityRegion[name.toLowerCase()] !== undefined
        return known ? "city:" + name : "region:" + name
      }
    }
    return ""
  }

  function placeOptionOf(args, title, subtitle) {
    // A recent from a place the option list can't rebuild: offered as-is so
    // editing keeps the place instead of silently changing it.
    for (var i = 0; i < args.length; i++) {
      if (args[i].charAt(0) !== "-" && args[i] !== "best") {
        var where = whereOf(args)
        var known = false
        for (var j = 0; j < whereOptions.length; j++) if (whereOptions[j].value === where) known = true
        if (!known) return { value: where, label: title || String(args[i]), description: subtitle || "" }
      }
    }
    return null
  }

  function setDraft(key, value) {
    if (draft === null) return
    var d = Object.assign({}, draft)
    d[key] = value
    draft = d
  }

  // Edit an existing profile, or start a new one where you are: the top of
  // Recent is the place you last asked for, which is usually the one worth
  // naming. Only the draft changes here; the state file waits for Save.
  function openEditor(profile) {
    clearHighlight()
    var src = profile ? profile : (vpn.recents.length > 0 ? vpn.recents[0] : null)
    var args = src && Array.isArray(src.args) ? src.args : []
    var where = whereOf(args)
    var d = {
      id: profile ? profile.id : vpn.newProfileId(),
      isNew: !profile,
      color: profile ? profile.color : "accent",
      where: where,
      placeOption: placeOptionOf(args, src ? src.title : "", src ? src.subtitle : "")
    }
    nameField.text = profile ? profile.name : ""
    draft = d
    editorIndex = 0
    editorButtonIndex = 0
    cursorActive = true
    focusSection = "editor"
    Qt.callLater(function() { if (nameField.visible) nameField.forceActiveFocus() })
  }

  function closeEditor(selectId) {
    draft = null
    keyCatcher.forceActiveFocus()
    focusSection = "profiles"
    profileIndex = vpn.profiles.length
    for (var i = 0; i < vpn.profiles.length; i++) if (vpn.profiles[i].id === selectId) profileIndex = i
    ensureCursor()
    scrollCursorIntoView()
  }

  function saveDraft() {
    if (draft === null) return
    var name = String(nameField.text || "").trim()
    if (name === "") { nameField.forceActiveFocus(); return }
    var p = vpn.buildProfile(draft.id, name, draft.color, draft.where)
    if (!p || !vpn.saveProfile(p)) return
    closeEditor(p.id)
  }

  function deleteDraft() {
    if (draft === null || draft.isNew) return
    vpn.deleteProfile(draft.id)
    closeEditor("")
  }

  function cycleColor(step) {
    if (draft === null) return
    var names = vpn.profileColors
    var i = Math.max(0, names.indexOf(draft.color))
    setDraft("color", names[(i + step + names.length) % names.length])
  }

  function activateEditorRow() {
    var row = editorRow
    if (row === "name") nameField.forceActiveFocus()
    else if (row === "color") cycleColor(1)
    else if (row === "where") whereRow.toggle()
    else if (row === "buttons") {
      if (editorButton === "save") saveDraft()
      else if (editorButton === "cancel") closeEditor(draft.id)
      else if (editorButton === "delete") deleteDraft()
    }
  }

  function hoverEditorButton(index) {
    if (!pointerMoved) return
    editorButtonIndex = index
    setCursor("editor", 3)
  }

  function stepEditorButton(step) {
    editorButtonIndex = Math.max(0, Math.min(editorButtons.length - 1, editorButtonIndex + step))
  }

  // Each row leads with a glyph in the foreground: hollow and dim until
  // that is the connection you are on, then filled and full, the way the
  // bar icon fills when the tunnel is up. The bolt is the engine's best
  // pick; history is wherever you last connected to.
  readonly property var quickActions: [
    { key: "best", label: "Best", hint: "The engine's pick for your network", icon: "\udb85\udc0b", outline: "\udb85\udc0c" },
    { key: "last", label: "Last used", hint: "Wherever you connected to last", icon: "\udb80\udeda", outline: "\udb80\udeda" }
  ]

  // Whether a Quick Connect row is the connection you are on. The engine
  // doesn't say which of the two a standing tunnel came from, so the rows
  // only light while the request that started it is the live one; after a
  // shell restart the header still carries the place itself.
  function quickActive(key) {
    if (!vpn.connected) return false
    return vpn.quickRequested === key
  }

  readonly property var filteredRegions: Model.filterRegions(vpn.regions, filterQuery)

  readonly property string heroMeta: {
    if (!vpn.installed) return vpn.installing ? "Installing…" : "Not installed"
    if (vpn.busy && vpn.pendingLabel !== "") return vpn.pendingLabel
    // The engine isn't answering: unknown, not a protection claim either
    // way (the bar icon dims with it).
    if (vpn.statusStale) return "Status unknown"
    if (vpn.connected) {
      var meta = vpn.displayPlace === "" ? "Protected" : vpn.displayPlace
      // Through a profile, the name you gave the place leads.
      var prof = vpn.activeProfileEntry
      return prof ? prof.name + " · " + meta : meta
    }
    if (vpn.interference && vpn.statusConnected) return "Connected · network interference detected"
    if (!vpn.accountProbed) return "Checking…"
    if (!vpn.signedIn) return "Signed out"
    return "Not protected"
  }

  readonly property string toggleHint: vpn.connected ? "Disconnect" : "Connect to the best location"

  function runQuick(key) {
    if (key === "best") vpn.connectBest()
    else if (key === "last") vpn.connectLast()
  }

  function sectionList() {
    if (!vpn.installed) return [{ name: "install", count: 1 }]
    if (!vpn.signedIn) return [{ name: "signin", count: 1 }]
    var list = [{ name: "header", count: 1 }]
    // The tab strip is one row: h and l pick the tab, the way they pick
    // a colour swatch in the editor.
    list.push({ name: "tabs", count: 1 })
    if (tab === "protection") {
      list.push({ name: "protection", count: protectionCount })
    } else {
      // Each list's header is a row of its own: it folds the list. The
      // profile rows carry a "New profile" row at the end, and the editor
      // takes the profile list's place, header and all.
      list.push({ name: "quickHeader", count: 1 })
      if (vpn.quickExpanded) list.push({ name: "quick", count: quickActions.length })
      if (vpn.statics.length > 0) {
        list.push({ name: "staticsHeader", count: 1 })
        if (vpn.staticsExpanded) list.push({ name: "statics", count: vpn.statics.length })
      }
      if (draft !== null) list.push({ name: "editor", count: editorRows.length })
      else {
        list.push({ name: "profilesHeader", count: 1 })
        if (vpn.profilesExpanded) list.push({ name: "profiles", count: vpn.profiles.length + 1 })
      }
      if (vpn.recents.length > 0) {
        list.push({ name: "recentsHeader", count: 1 })
        if (vpn.recentsExpanded) list.push({ name: "recents", count: vpn.recents.length })
      }
      if (drilled) list.push({ name: "cities", count: cityRowCount })
      else {
        list.push({ name: "regionsHeader", count: 1 })
        if (vpn.countriesExpanded && filteredRegions.length > 0) list.push({ name: "regions", count: filteredRegions.length })
      }
    }
    return list
  }

  function sectionIndex(name) {
    if (name === "quick") return quickIndex
    if (name === "statics") return staticIndex
    if (name === "protection") return protectionIndex
    if (name === "profiles") return profileIndex
    if (name === "editor") return editorIndex
    if (name === "recents") return recentIndex
    if (name === "regions") return regionIndex
    if (name === "cities") return cityIndex
    return 0
  }

  function setSectionIndex(name, value) {
    if (name === "quick") quickIndex = value
    else if (name === "statics") staticIndex = value
    else if (name === "protection") protectionIndex = value
    else if (name === "profiles") profileIndex = value
    else if (name === "editor") editorIndex = value
    else if (name === "recents") recentIndex = value
    else if (name === "regions") regionIndex = value
    else if (name === "cities") cityIndex = value
  }

  // Keep the cursor on a section that exists, with an index inside its rows.
  function ensureCursor() {
    var list = sectionList()
    var pos = -1
    for (var i = 0; i < list.length; i++) if (list[i].name === focusSection) pos = i
    if (pos === -1) {
      // Regions and cities stand in for each other across a drill, and
      // a folded list hands its cursor to the header that folded it.
      if (focusSection === "regions" && drilled) focusSection = "cities"
      else if (focusSection === "cities" && !drilled) focusSection = vpn.countriesExpanded ? "regions" : "regionsHeader"
      else if (focusSection === "regions") focusSection = "regionsHeader"
      else if (focusSection === "quick") focusSection = "quickHeader"
      else if (focusSection === "statics") focusSection = "staticsHeader"
      else if (focusSection === "profiles") focusSection = "profilesHeader"
      else if (focusSection === "recents") focusSection = "recentsHeader"
      else focusSection = list[0].name
      for (i = 0; i < list.length; i++) if (list[i].name === focusSection) pos = i
      if (pos === -1) { focusSection = list[0].name; pos = 0 }
    }
    var count = list[pos].count
    setSectionIndex(focusSection, Math.max(0, Math.min(count - 1, sectionIndex(focusSection))))
  }

  // Put the Regions section header at the top of the view, and keep it
  // there while the content underneath is still changing shape.
  //
  // A drill swaps 76 region rows for a short placeholder, then the city
  // list arrives a beat later. Each of those changes the Flickable's content
  // height, and the Flickable clamps contentY to the new height the moment it
  // learns it, which is *after* any code that ran on the click. So the
  // anchor can't be a one-shot: it stays pending, is re-applied on every
  // contentHeight change, and is dropped the instant the person scrolls.
  property bool anchorPending: false

  function anchorRegionSection() {
    anchorPending = true
    applyAnchor()
    Qt.callLater(applyAnchor)
  }

  function highlightRow() {
    if (!highlight || !drilled || drilledName !== highlight.region || !cityColumn) return null
    if (!drilledRegion) return null
    for (var i = 0; i < drilledRegion.cities.length; i++) {
      if (drilledRegion.cities[i].name === highlight.city) return cityColumn.children[i + 1] || null
    }
    return null
  }

  function applyAnchor() {
    if (!anchorPending || !panelFlick || !regionSection) return
    var target = regionSection.y
    // A map-clicked city sits just under the region header, so both the
    // "where am I" context and the pulsing row are on screen together.
    var row = highlightRow()
    if (row) target = Math.max(regionSection.y, row.mapToItem(panelFlick.contentItem, 0, 0).y - Style.space(110))
    panelFlick.contentY = Math.max(0, Math.min(root.maxScroll(), target))
  }

  function clearHighlight() { highlight = null }

  function drillInto(regionName, city) {
    if (String(regionName || "") === "") return
    // The map can be clicked from either tab; the city list lives on
    // Connections, so go there first or the drill lands on nothing.
    if (tab !== "connections") setTab("connections")
    cursorActive = true
    drilledName = String(regionName)
    focusSection = "cities"
    cityIndex = 0
    if (city !== undefined && city !== null) highlight = { region: drilledName, city: String(city) }
    anchorRegionSection()
  }

  function drillOut() {
    clearHighlight()
    drilledName = ""
    focusSection = "regions"
    cityIndex = 0
    anchorRegionSection()
  }

  function moveCursor(dx, dy) {
    pointerMoved = false
    cursorActive = true
    ensureCursor()
    if (dy !== 0) anchorPending = false

    // Horizontal moves drill in and out of a region's city list. In the
    // editor they walk the colour swatches.
    if (dx !== 0) {
      if (focusSection === "editor") {
        if (editorRow === "color") cycleColor(dx)
        else if (editorRow === "buttons") stepEditorButton(dx)
        return
      }
      if (focusSection === "tabs") { stepTab(dx); return }
      if (dx > 0 && focusSection === "regions") drillInto(filteredRegions[regionIndex] ? filteredRegions[regionIndex].name : "")
      else if (dx < 0 && focusSection === "cities") drillOut()
      // On a header they unfold and fold its list, the way they open and
      // close a region.
      else if (focusSection === "quickHeader") setQuickExpanded(dx > 0)
      else if (focusSection === "staticsHeader") setStaticsExpanded(dx > 0)
      else if (focusSection === "profilesHeader") setProfilesExpanded(dx > 0)
      else if (focusSection === "recentsHeader") setRecentsExpanded(dx > 0)
      else if (focusSection === "regionsHeader") setCountriesExpanded(dx > 0)
      return
    }
    if (dy === 0) return

    var list = sectionList()
    var pos = 0
    for (var i = 0; i < list.length; i++) if (list[i].name === focusSection) pos = i
    var next = sectionIndex(focusSection) + dy

    if (next < 0) {
      if (pos === 0) return
      focusSection = list[pos - 1].name
      setSectionIndex(focusSection, list[pos - 1].count - 1)
    } else if (next >= list[pos].count) {
      if (pos === list.length - 1) return
      focusSection = list[pos + 1].name
      setSectionIndex(focusSection, 0)
    } else {
      setSectionIndex(focusSection, next)
    }
    if (focusSection === "header") { if (panelFlick) panelFlick.contentY = 0 }
    else scrollCursorIntoView()
  }

  function activateCursor() {
    clearHighlight()
    ensureCursor()
    if (focusSection === "install") vpn.installCli()
    else if (focusSection === "signin") submitSignIn()
    else if (focusSection === "header") vpn.toggle()
    else if (focusSection === "quickHeader") setQuickExpanded(!vpn.quickExpanded)
    else if (focusSection === "quick") runQuick(quickActions[quickIndex].key)
    else if (focusSection === "staticsHeader") setStaticsExpanded(!vpn.staticsExpanded)
    else if (focusSection === "statics") { vpn.connectStatic(vpn.statics[staticIndex]); showConnection() }
    else if (focusSection === "protection") {
      if (protectionIndex === 0) vpn.toggleFirewall()
      else if (protectionIndex === 1) vpn.toggleAlwaysOnFirewall()
      else if (protectionIndex === 2) vpn.toggleAutoConnect()
      else if (protectionIndex === 3) vpn.toggleSplitTunnel()
      else if (splitDetailVisible && protectionIndex === 4) splitModeRow.toggle()
      else if (splitDetailVisible && protectionIndex === 5) splitAppsRow.toggle()
      else if (protectionIndex === lanIndex) vpn.toggleLanTraffic()
      else requestSignOut()
    }
    else if (focusSection === "profilesHeader") setProfilesExpanded(!vpn.profilesExpanded)
    else if (focusSection === "profiles") {
      if (profileIndex < vpn.profiles.length) { vpn.connectProfile(vpn.profiles[profileIndex].id); showConnection() }
      else openEditor(null)
    }
    else if (focusSection === "editor") activateEditorRow()
    else if (focusSection === "recentsHeader") setRecentsExpanded(!vpn.recentsExpanded)
    else if (focusSection === "recents") { vpn.connectRecent(recentIndex); showConnection() }
    // Enter on a region opens its cities rather than connecting blind;
    // the first row inside is still "Fastest in <region>", so the old
    // one-keystroke behaviour is only ever one row away.
    else if (focusSection === "regionsHeader") setCountriesExpanded(!vpn.countriesExpanded)
    else if (focusSection === "regions") drillInto(filteredRegions[regionIndex] ? filteredRegions[regionIndex].name : "")
    else if (focusSection === "cities") activateCityRow(cityIndex)
  }

  // The region list stays folded until asked for, and stays how it was
  // left, across opens and shell restarts. Unfolding puts its header at the
  // top of the view, the way a drill does, so the rows fill the panel.
  function setCountriesExpanded(on) {
    if (vpn.countriesExpanded === on) return
    vpn.setCountriesExpanded(on)
    if (on) anchorRegionSection()
  }

  // Quick Connect, Static IPs, Profiles and Recent fold the same way, but
  // sit near the top of the panel and hold a handful of rows, so unfolding
  // them moves nothing.
  function setQuickExpanded(on) { vpn.setQuickExpanded(on) }
  function setStaticsExpanded(on) { vpn.setStaticsExpanded(on) }
  function setProfilesExpanded(on) { vpn.setProfilesExpanded(on) }
  function setRecentsExpanded(on) { vpn.setRecentsExpanded(on) }

  // ── Vim motions ─────────────────────────────────────────────────────────
  // The shell's key catcher already turns hjkl and the arrows into moves;
  // the rest of the vocabulary lands here as single letters. "g" and "z"
  // are prefixes and wait a beat for their second key, the way Vim does.
  property string pendingKey: ""
  readonly property int pageRows: 5

  Timer {
    id: pendingTimer
    interval: 1000
    onTriggered: root.pendingKey = ""
  }

  function clearPending() {
    pendingKey = ""
    pendingTimer.stop()
  }

  function textKey(t) {
    var pending = pendingKey
    clearPending()
    if (pending === "g") {
      if (t === "g") jumpTop()
      else if (t === "t") stepTab(1)
      else if (t === "T") stepTab(-1)
      return
    }
    if (pending === "z") {
      if (t === "a") foldUnderCursor("toggle")
      else if (t === "o") foldUnderCursor("open")
      else if (t === "c") foldUnderCursor("close")
      else if (t === "R") setAllFolds(true)
      else if (t === "M") setAllFolds(false)
      return
    }
    if (t === "g" || t === "z") { pendingKey = t; pendingTimer.restart(); return }
    // The filter lives inside the list, so "/" unfolds it first.
    if (t === "/") { setCountriesExpanded(true); filterField.forceActiveFocus() }
    else if (t === "G") jumpBottom()
    else if (t === "{") jumpSection(-1)
    else if (t === "}") jumpSection(1)
    // Ctrl-d and Ctrl-u arrive as the control characters they are.
    else if (t === "\u0004") moveRows(pageRows)
    else if (t === "\u0015") moveRows(-pageRows)
    else if (t === "q") close()
    else if (t === "e" && cursorActive && focusSection === "profiles"
             && profileIndex < vpn.profiles.length) openEditor(vpn.profiles[profileIndex])
  }

  function jumpTop() {
    cursorActive = true
    focusSection = "header"
    ensureCursor()
    anchorPending = false
    if (panelFlick) panelFlick.contentY = 0
  }

  function jumpBottom() {
    cursorActive = true
    var list = sectionList()
    var last = list[list.length - 1]
    focusSection = last.name
    setSectionIndex(last.name, last.count - 1)
    ensureCursor()
    anchorPending = false
    scrollCursorIntoView()
  }

  // "{" and "}" step a whole section at a time, landing on its first row.
  function jumpSection(step) {
    cursorActive = true
    ensureCursor()
    var list = sectionList()
    var pos = 0
    for (var i = 0; i < list.length; i++) if (list[i].name === focusSection) pos = i
    var next = Math.max(0, Math.min(list.length - 1, pos + step))
    focusSection = list[next].name
    setSectionIndex(focusSection, 0)
    anchorPending = false
    if (focusSection === "header") { if (panelFlick) panelFlick.contentY = 0 }
    else scrollCursorIntoView()
  }

  function moveRows(n) {
    var step = n > 0 ? 1 : -1
    for (var i = 0; i < Math.abs(n); i++) moveCursor(0, step)
  }

  // Which fold a section belongs to: its header row or the rows under it.
  function foldOf(section) {
    if (section === "quickHeader" || section === "quick") return "quick"
    if (section === "staticsHeader" || section === "statics") return "statics"
    if (section === "profilesHeader" || section === "profiles") return "profiles"
    if (section === "recentsHeader" || section === "recents") return "recents"
    if (section === "regionsHeader" || section === "regions") return "countries"
    return ""
  }

  function foldExpanded(name) {
    if (name === "quick") return vpn.quickExpanded
    if (name === "statics") return vpn.staticsExpanded
    if (name === "profiles") return vpn.profilesExpanded
    if (name === "recents") return vpn.recentsExpanded
    return vpn.countriesExpanded
  }

  function setFold(name, on) {
    if (name === "quick") setQuickExpanded(on)
    else if (name === "statics") setStaticsExpanded(on)
    else if (name === "profiles") setProfilesExpanded(on)
    else if (name === "recents") setRecentsExpanded(on)
    else if (name === "countries") setCountriesExpanded(on)
  }

  // za, zo, zc: the fold the cursor is in, whether on its header or a row
  // under it. Folding from inside hands the cursor to the header.
  function foldUnderCursor(mode) {
    cursorActive = true
    ensureCursor()
    var f = foldOf(focusSection)
    if (f === "") return
    var on = mode === "toggle" ? !foldExpanded(f) : mode === "open"
    setFold(f, on)
    ensureCursor()
    scrollCursorIntoView()
  }

  // zR and zM: every fold at once.
  function setAllFolds(on) {
    cursorActive = true
    var names = ["quick", "statics", "profiles", "recents", "countries"]
    for (var i = 0; i < names.length; i++) setFold(names[i], on)
    ensureCursor()
    scrollCursorIntoView()
  }

  function activateCityRow(index) {
    if (!drilledRegion) return
    if (index <= 0) {
      vpn.connectRegion(drilledName)
    } else {
      var c = drilledRegion.cities[index - 1]
      if (!c) return
      vpn.connectCity(c.name, drilledName)
    }
    showConnection()
  }

  // Empty field: put the cursor in it rather than opening a terminal that
  // would only ask the same question.
  function submitSignIn() {
    var u = String(usernameField.text || "").trim()
    if (u === "") { usernameField.forceActiveFocus(); return }
    if (vpn.signIn(u)) keyCatcher.forceActiveFocus()
  }

  function maxScroll() {
    return panelFlick ? Math.max(0, panelFlick.contentHeight - panelFlick.height) : 0
  }

  // After picking a place to connect to, bring the map and connection
  // details back into view, that's what the person wants to watch next.
  function showConnection() {
    clearHighlight()
    anchorPending = false
    cursorActive = false
    focusSection = "header"
    if (panelFlick) panelFlick.contentY = 0
  }

  function requestSignOut() {
    if (!signOutArmed) { signOutArmed = true; signOutArm.restart(); return }
    signOutArmed = false
    vpn.signOut()
  }

  Timer { id: signOutArm; interval: 5000; onTriggered: root.signOutArmed = false }

  // l and h on the tab row, gt and gT anywhere: the next tab along, and
  // the strip stops at its ends.
  function stepTab(step) {
    var i = 0
    for (var k = 0; k < tabs.length; k++) if (tabs[k].key === tab) i = k
    var next = Math.max(0, Math.min(tabs.length - 1, i + step))
    if (next !== i) setTab(tabs[next].key)
  }

  function setTab(key) {
    if (key !== "protection" && key !== "connections") return
    clearHighlight()
    tab = key
    anchorPending = false
    ensureCursor()
  }

  function setCursor(section, index) {
    cursorActive = true
    focusSection = section
    setSectionIndex(section, index)
  }

  function setHeaderCursor() {
    setCursor("header", 0)
    if (panelFlick) panelFlick.contentY = 0
  }

  function scrollItemIntoView(item) {
    if (!panelFlick || !item) return
    Qt.callLater(function() {
      if (!item) return
      var margin = Style.space(6)
      var point = item.mapToItem(panelFlick.contentItem, 0, 0)
      var top = point.y
      var bottom = top + item.height
      var viewTop = panelFlick.contentY
      var viewBottom = viewTop + panelFlick.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < viewTop + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > viewBottom - margin) panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  function scrollCursorIntoView() {
    var column = null
    if (focusSection === "quickHeader") column = quickSection
    else if (focusSection === "quick") column = quickColumn
    else if (focusSection === "tabs") column = tabRow
    else if (focusSection === "protection") column = protectionColumn
    else if (focusSection === "staticsHeader") column = staticsSection
    else if (focusSection === "statics") column = staticsColumn
    else if (focusSection === "profilesHeader") column = profileSection
    else if (focusSection === "profiles") column = profileColumn
    else if (focusSection === "editor") column = editorColumn
    else if (focusSection === "recentsHeader") column = recentSection
    else if (focusSection === "recents") column = recentColumn
    else if (focusSection === "regionsHeader") column = regionSection
    else if (focusSection === "regions") column = regionColumn
    else if (focusSection === "cities") column = cityColumn
    var i = sectionIndex(focusSection)
    // "New profile" is the last child of its column, after the Repeater.
    if (focusSection === "profiles" && i >= vpn.profiles.length && column) i = column.children.length - 1
    // The editor's buttons share one row, the last child of its column.
    if (focusSection === "editor" && i === 3 && column) i = column.children.length - 1
    // The Protection column carries the Account header and rows after its
    // switches; the sign-out row is its last child wherever the cursor for it
    // has ended up.
    if (focusSection === "protection" && i === signOutIndex && column) i = column.children.length - 1
    if (column && i >= 0 && i < column.children.length) scrollItemIntoView(column.children[i])
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    vpn.panelOpen = opened
    if (opened) {
      highlight = null
      anchorPending = false
      cursorActive = false
      filterQuery = ""
      draft = null
      paletteFile.reload()
      drilledName = ""
      cityIndex = 0
      if (panelFlick) panelFlick.contentY = 0
      ensureCursor()
      Qt.callLater(function() { keyCatcher.forceActiveFocus() })
    }
  }

  Service {
    id: vpn
    settings: root.settings
  }

  Connections {
    target: vpn
    function onSignedInChanged() { root.ensureCursor() }
    function onInstalledChanged() { root.ensureCursor() }
    function onRegionsChanged() { root.ensureCursor() }
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function connect(): string { vpn.connectBest(); return "ok" }
    function disconnect(): string { vpn.disconnect(); return "ok" }
    function refresh(): string { vpn.refresh(); return "ok" }
    // The same Service calls the panel rows make, exposed for scripting.
    // Each one runs the identical code path a click on the row runs.
    function firewall(): string { vpn.toggleFirewall(); return vpn.firewallState }
    function connectStatic(ip: string): string {
      var want = String(ip || "")
      for (var i = 0; i < vpn.statics.length; i++) {
        if (vpn.statics[i].ip === want) { vpn.connectStatic(vpn.statics[i]); return "ok" }
      }
      vpn.connectStatic({ city: "", ip: want })
      return "ok"
    }
    function splitTunnel(): string { vpn.toggleSplitTunnel(); return vpn.splitOn ? "on" : "off" }
    function status(): string { return vpn.displayStatus + (vpn.displayPlace !== "" ? ": " + vpn.displayPlace : "") }
    // View-only controls: they change what the panel shows, never the tunnel.
    function tab(key: string): string { root.setTab(String(key)); return root.tab }
    function drill(region: string): string {
      var name = String(region || "")
      if (name === "") { root.drillOut(); return "ok" }
      root.drillInto(name)
      return "ok"
    }
    function connectCity(name: string): string {
      vpn.connectCity(name, "")
      return "ok"
    }
    // "tabs" scrolls the tab strip to the top; a number is an absolute contentY.
    function scroll(to: string): string {
      if (!panelFlick) return "no panel"
      var y = String(to) === "tabs" ? tabRow.y : parseFloat(to)
      if (!isFinite(y)) return "bad value"
      root.anchorPending = false
      panelFlick.contentY = Math.max(0, Math.min(root.maxScroll(), y))
      return String(Math.round(panelFlick.contentY))
    }
    function highlight(region: string, city: string): string {
      root.drillInto(String(region), String(city))
      return "ok"
    }
    // Deliberately omits nothing sensitive because the CLI never prints the
    // account name: status carries only usage and plan.
    function debug(): string {
      return JSON.stringify({
        installed: vpn.installed,
        accountProbed: vpn.accountProbed,
        signedIn: vpn.signedIn,
        connected: vpn.connected,
        busy: vpn.busy,
        firewall: vpn.firewallState,
        firewallMode: vpn.firewallMode,
        splitTunneling: vpn.splitOn ? vpn.splitMode : "off",
        splitApps: vpn.splitApps.length,
        lanTraffic: vpn.lanTrafficOn,
        regions: vpn.regions.length,
        regionsExpanded: vpn.countriesExpanded,
        quickExpanded: vpn.quickExpanded,
        statics: vpn.statics.length,
        profiles: vpn.profiles.length,
        profilesExpanded: vpn.profilesExpanded,
        recents: vpn.recents.length,
        recentsExpanded: vpn.recentsExpanded,
        cities: vpn.cities.length,
        currentPlace: vpn.currentPlace ? vpn.currentPlace.city : "",
        traffic: { device: vpn.linkDevice, samples: vpn.rxHistory.length, rx: vpn.rxRate, tx: vpn.txRate },
        stateLoaded: vpn.stateLoaded,
        autoConnect: vpn.autoConnect,
        autoReady: vpn.autoReady,
        autoLastError: vpn.autoLastError,
        statusConnected: vpn.statusConnected,
        statusStale: vpn.statusStale,
        desired: vpn._desired,
        autoWaitMs: Math.max(0, vpn._autoNextMs - Date.now()),
        pinFailed: vpn._autoPinFailed,
        drilledInto: root.drilledName,
        dataUsage: vpn.dataUsage,
        lastCommand: vpn.lastCommand,
        lastError: vpn.lastError
      })
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: Component {
      Item {
        WindIcon {
          anchors.centerIn: parent
          iconSize: Style.space(13)
          color: root.barIconColor
          opacity: vpn.connected ? 1.0 : 0.6
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) vpn.toggle()
      else if (buttonCode === Qt.MiddleButton) vpn.refresh()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(580))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // A text field owns the keyboard while it has focus, otherwise every
      // letter typed would drive the cursor instead of the input.
      // An open Mode or Apps popup owns the keyboard while it is up, or hjkl
      // would drive the cursor on the panel behind it, scrolling a list the
      // person can't see instead of moving inside the one they opened.
      blocked: filterField.activeFocus || usernameField.activeFocus || nameField.activeFocus
               || splitModeRow.popupOpen || splitAppsRow.popupOpen
               || whereRow.popupOpen
      onMoveRequested: function(dx, dy) {
        root.clearPending()
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: {
        root.clearPending()
        if (root.cursorActive) root.activateCursor()
      }
      // Inside a drill, escape backs out one level before it closes the panel.
      // Inside the editor it drops the draft, the same way it backs out
      // of a drill.
      onCloseRequested: {
        if (root.draft !== null) { root.closeEditor(root.draft.id); return }
        root.drilled ? root.drillOut() : root.close()
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      // No single-letter actions on the tunnel, on purpose: the panel takes
      // keyboard focus when it opens, and a stray keystroke must never change
      // it. The letters move the cursor, fold lists, switch tabs or close the
      // panel; "e" only opens the editor for the profile under the cursor.
      onTextKey: function(t) { root.textKey(t) }

      // What counts as the pointer actually moving. A row sliding under a
      // still pointer reports a hover but moves no pointer, and this is how
      // the panel tells the two apart. A handler rather than a MouseArea so
      // it sees the movement without taking hover away from the rows.
      HoverHandler {
        property real lastX: -1
        property real lastY: -1
        onPointChanged: {
          var pos = point.position
          if (Math.abs(pos.x - lastX) > 1 || Math.abs(pos.y - lastY) > 1) {
            lastX = pos.x
            lastY = pos.y
            root.pointerMoved = true
          }
        }
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        onContentHeightChanged: root.applyAnchor()
        onMovementStarted: root.anchorPending = false

        // Fixed steps instead of Flickable's momentum: one wheel notch moves
        // about a row and a half, touchpads scroll by their pixel delta, and
        // the content never overshoots or coasts.
        WheelHandler {
          target: null
          acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
          onWheel: function(event) {
            root.anchorPending = false
            var dy = event.pixelDelta.y !== 0 ? event.pixelDelta.y : (event.angleDelta.y / 120) * Style.space(72)
            panelFlick.contentY = Math.max(0, Math.min(root.maxScroll(), panelFlick.contentY - dy))
            event.accepted = true
          }
        }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          Item {
            id: header
            width: parent.width
            implicitHeight: hero.implicitHeight
            // The hero's trailingControl resolves `root` to PanelHero, so
            // panel state is reached through `header` instead.
            readonly property bool ringVisible: root.cursorActive && root.focusSection === "header"
            function focusHero() { root.setHeaderCursor() }

            PanelHero {
              id: hero
              width: parent.width
              title: "Windscribe"
              meta: root.heroMeta
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconOpacity: vpn.connected ? 1.0 : 0.5
              iconComponent: Component {
                WindIcon {
                  iconSize: Style.font.display
                  color: root.iconColor
                }
              }

              trailingControl: Component {
                ToggleSwitch {
                  id: powerSwitch
                  // A live tunnel is enough to show the switch: you can always
                  // disconnect, even if the status probe hasn't landed.
                  visible: vpn.installed && (vpn.signedIn || vpn.connected)
                  checked: vpn.connected
                  busy: vpn.busy
                  hasCursor: header.ringVisible
                  foreground: hero.foreground
                  onHovered: function(on) { if (on) header.focusHero() }
                  onToggled: { root.clearHighlight(); vpn.toggle() }

                  PanelToolTip {
                    visible: powerSwitch.containsMouse
                    text: root.toggleHint
                    fontFamily: hero.fontFamily
                  }
                }
              }
            }
          }

          // ── Map ─────────────────────────────────────────────────────────
          WorldMap {
            visible: vpn.signedIn && vpn.cities.length > 0
            width: parent.width
            cities: vpn.cities
            current: vpn.currentPlace
            connected: vpn.connected
            foreground: root.foreground
            fontFamily: root.fontFamily
            onCityClicked: function(c) {
              var region = vpn.cityRegion[String(c.city).toLowerCase()] || ""
              if (region !== "") {
                root.drillInto(region, c.city)
              } else {
                // A city the location list no longer carries: connect to it
                // by name rather than failing silently.
                vpn.connectCity(c.city, "")
                root.showConnection()
              }
            }
          }

          Text {
            visible: vpn.actionStatus !== "" || vpn.lastError !== ""
            width: parent.width
            text: vpn.actionStatus !== "" ? vpn.actionStatus : vpn.lastError
            textFormat: Text.PlainText
            color: vpn.lastError !== "" && vpn.actionStatus === "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          // ── First run: the CLI isn't there yet ──────────────────────────
          Column {
            visible: !vpn.installed
            width: parent.width
            spacing: Style.space(8)

            ActionRow {
              width: parent.width
              hasCursor: root.cursorActive && root.focusSection === "install"
              icon: "󰇚"
              title: vpn.installing ? "Installing Windscribe CLI…" : "Install Windscribe CLI"
              subtitle: vpn.installing ? "Finish the install in the terminal that opened" : "Opens a terminal with the install steps"
              enabled: !vpn.installing
              onEntered: root.setCursorFromHover("install", 0)
              onClicked: vpn.installCli()
            }

            Text {
              width: parent.width
              text: "You'll also need a Windscribe account. A free one works: sign up at windscribe.com if you don't have one yet."
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          // ── Sign in ─────────────────────────────────────────────────────
          Column {
            // Held back until the status probe lands, and never shown over a
            // live tunnel, a connection is proof the session is valid.
            visible: vpn.installed && vpn.accountProbed && !vpn.signedIn && !vpn.connected
            width: parent.width
            spacing: Style.space(8)

            TextField {
              id: usernameField
              width: parent.width
              foreground: root.foreground
              placeholderText: "Windscribe username or email"
              Keys.onEscapePressed: function(event) {
                keyCatcher.forceActiveFocus()
                event.accepted = true
              }
              Keys.onReturnPressed: function(event) {
                root.submitSignIn()
                event.accepted = true
              }
            }

            ActionRow {
              width: parent.width
              hasCursor: root.cursorActive && root.focusSection === "signin"
              icon: "󰌆"
              title: "Log in with Windscribe"
              subtitle: "Opens a terminal for your password and 2FA code"
              onEntered: root.setCursorFromHover("signin", 0)
              onClicked: root.submitSignIn()
            }
          }

          // ── Connection detail ───────────────────────────────────────────
          // The place is promoted to the hero; the protocol, the IP and the
          // remaining status rows are rendered here.
          Column {
            visible: vpn.connected
            width: parent.width
            spacing: Style.spacing.labelGap

            InfoPair { visible: vpn.protocolLine !== ""; label: "Protocol"; value: vpn.protocolLine }
            InfoPair { visible: vpn.ipLine !== ""; label: "IP"; value: vpn.ipLine }
            InfoPair { visible: vpn.checkingConnection; label: "Connection"; value: "still checking the connection" }
            InfoPair { visible: vpn.firewallState !== ""; label: "Firewall"; value: vpn.firewallState }

            Repeater {
              model: {
                var out = []
                for (var i = 0; i < vpn.fields.length; i++) {
                  var f = vpn.fields[i]
                  var key = String(f.label).toLowerCase()
                  // Protocol and IP have their own rows above; firewall
                  // lives here too, data usage and update on Protection.
                  if (key === "protocol" || key === "ip" || key === "firewall"
                      || key === "data usage" || key === "update") continue
                  out.push(f)
                }
                return out
              }
              InfoPair {
                required property var modelData
                label: modelData.label
                value: modelData.value
              }
            }

            ActionRow {
              width: parent.width
              hasCursor: false
              icon: "\udb81\udc9f"
              iconColor: root.dim
              title: "Rotate IP"
              subtitle: "A different IP on the same server"
              enabled: !vpn.busy
              onClicked: { root.clearHighlight(); vpn.rotateIp() }
            }

            ActionRow {
              visible: vpn.updateAvailable !== ""
              width: parent.width
              hasCursor: false
              icon: "󰇚"
              iconColor: root.urgent
              title: "Windscribe " + vpn.updateAvailable
              subtitle: "Update available — opens a terminal to install it"
              onClicked: {
                Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", "windscribe-cli update"])
              }
            }
          }

          // ── Traffic ─────────────────────────────────────────────────────
          Traffic {
            // Only over a live, settled tunnel: not while connecting or
            // disconnecting, and gone the instant a disconnect is asked for.
            visible: vpn.connected && vpn.linkDevice !== "" && !vpn.busy && vpn.rxHistory.length > 0
            width: parent.width
            rxHistory: vpn.rxHistory
            txHistory: vpn.txHistory
            rxRate: vpn.rxRate
            txRate: vpn.txRate
            sessionRx: vpn.sessionRx
            sessionTx: vpn.sessionTx
            uptimeSec: vpn.uptimeSec
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          PanelSeparator {
            visible: vpn.signedIn
            foreground: root.foreground
          }

          // ── Tabs ────────────────────────────────────────────────────────
          // Same pill strip as the network panel's DNS provider row.
          Row {
            id: tabRow
            visible: vpn.signedIn
            width: parent.width
            spacing: Style.space(6)
            readonly property real cellWidth: (width - spacing * (root.tabs.length - 1)) / root.tabs.length

            Repeater {
              model: root.tabs
              TabPill {
                required property var modelData
                required property int index
                width: tabRow.cellWidth
                tabKey: modelData.key
                text: modelData.label
              }
            }
          }

          // ── Quick connect ───────────────────────────────────────────────
          // First thing on the Connections tab, folded behind its header
          // like the lists under it.
          Column {
            id: quickSection
            visible: vpn.signedIn && root.tab === "connections"
            width: parent.width
            spacing: Style.space(10)

            FoldHeader {
              section: "quickHeader"
              title: "QUICK CONNECT"
              count: root.quickActions.length
              expanded: vpn.quickExpanded
              onToggled: root.setQuickExpanded(!vpn.quickExpanded)
            }

            Column {
              id: quickColumn
              visible: vpn.quickExpanded
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: root.quickActions
                ActionRow {
                  required property var modelData
                  required property int index
                  width: quickColumn.width
                  hasCursor: root.cursorActive && root.focusSection === "quick" && root.quickIndex === index
                  icon: root.quickActive(modelData.key) ? modelData.icon : modelData.outline
                  iconColor: root.quickActive(modelData.key) ? root.foreground : root.dim
                  title: modelData.label
                  subtitle: modelData.hint
                  enabled: !vpn.busy
                  onEntered: root.setCursorFromHover("quick", index)
                  onClicked: root.runQuick(modelData.key)
                }
              }
            }
          }

          // ── Static IPs ──────────────────────────────────────────────────
          // The account's own static IPs: one click each, the IP shown so
          // it can be checked against what a client expects.
          Column {
            id: staticsSection
            visible: vpn.signedIn && root.tab === "connections" && vpn.statics.length > 0
            width: parent.width
            spacing: Style.space(10)

            FoldHeader {
              section: "staticsHeader"
              title: "STATIC IPS"
              count: vpn.statics.length
              expanded: vpn.staticsExpanded
              onToggled: root.setStaticsExpanded(!vpn.staticsExpanded)
            }

            Column {
              id: staticsColumn
              visible: vpn.staticsExpanded
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: vpn.statics
                ActionRow {
                  required property var modelData
                  required property int index
                  width: staticsColumn.width
                  hasCursor: root.cursorActive && root.focusSection === "statics" && root.staticIndex === index
                  icon: "\udb82\ude5f"
                  title: modelData.city
                  subtitle: modelData.ip + " · your static IP"
                  trailing: vpn.connected && vpn.currentWhere.indexOf(modelData.city) !== -1 ? "\udb80\udd2c" : ""
                  enabled: !vpn.busy
                  onEntered: root.setCursorFromHover("statics", index)
                  onClicked: { vpn.connectStatic(modelData); root.showConnection() }
                }
              }
            }
          }

          // ── Protection ──────────────────────────────────────────────────
          Column {
            visible: vpn.signedIn && root.tab === "protection"
            width: parent.width
            spacing: Style.space(10)

            Column {
              id: protectionColumn
              width: parent.width
              spacing: Style.space(6)

              Toggle {
                width: parent.width
                label: "Firewall"
                // Says what the click will do, and why it may be locked.
                description: {
                  if (vpn.busy && vpn.pendingLabel !== "") return "Wait for the current action…"
                  if (!vpn.confLoaded) return "Blocks all traffic outside the VPN"
                  if (vpn.firewallAlwaysOn) return "Always on — turn off Always-on firewall to change it"
                  if (vpn.firewallState === "On") return "On now, blocking traffic outside the VPN"
                  return "Blocks all traffic outside the VPN, right now"
                }
                checked: String(vpn.firewallState).toLowerCase() === "on" || vpn.firewallAlwaysOn
                enabled: vpn.installed && !vpn.firewallAlwaysOn && !vpn.busy
                hasCursor: root.cursorActive && root.focusSection === "protection" && root.protectionIndex === 0
                foreground: root.foreground
                fontFamily: root.fontFamily
                onHovered: function(on) { if (on) root.setCursorFromHover("protection", 0) }
                onClicked: { root.clearHighlight(); vpn.toggleFirewall() }
              }

              Toggle {
                width: parent.width
                label: "Always-on firewall"
                description: {
                  var raced = vpn.confRacedNote("FirewallMode")
                  if (raced !== "") return raced
                  if (!vpn.confLoaded) return "Loading…"
                  if (!vpn.splitAvailable) return "Windscribe hasn't written its settings yet"
                  if (vpn.firewallAlwaysOn) return "Never goes down, even when disconnected"
                  return "Keep the firewall up even when the VPN is off"
                }
                checked: vpn.firewallAlwaysOn
                enabled: vpn.splitAvailable && !vpn.busy
                hasCursor: root.cursorActive && root.focusSection === "protection" && root.protectionIndex === 1
                foreground: root.foreground
                fontFamily: root.fontFamily
                onHovered: function(on) { if (on) root.setCursorFromHover("protection", 1) }
                onClicked: { root.clearHighlight(); vpn.toggleAlwaysOnFirewall() }
              }

              // Widget-owned, unlike the two above: the engine has no
              // setting for this, so it lives in the widget's own state file.
              Toggle {
                width: parent.width
                label: "Always On"
                description: "Reconnects on boot and interruptions"
                checked: vpn.autoConnect
                enabled: !vpn.busy
                hasCursor: root.cursorActive && root.focusSection === "protection" && root.protectionIndex === 2
                foreground: root.foreground
                fontFamily: root.fontFamily
                onHovered: function(on) { if (on) root.setCursorFromHover("protection", 2) }
                onClicked: { root.clearHighlight(); vpn.toggleAutoConnect() }
              }

              Toggle {
                width: parent.width
                label: "Split tunneling"
                description: vpn.splitDescription()
                checked: vpn.splitOn
                enabled: vpn.splitAvailable && !vpn.busy
                hasCursor: root.cursorActive && root.focusSection === "protection" && root.protectionIndex === 3
                foreground: root.foreground
                fontFamily: root.fontFamily
                onHovered: function(on) { if (on) root.setCursorFromHover("protection", 3) }
                onClicked: { root.clearHighlight(); vpn.toggleSplitTunnel() }
              }

              Dropdown {
                id: splitModeRow
                visible: root.splitDetailVisible
                width: parent.width
                label: "Mode"
                value: vpn.splitMode
                options: [
                  { value: "Exclude", label: "Exclude: chosen apps skip the VPN" },
                  { value: "Include", label: "Include: only chosen apps use the VPN" }
                ]
                hasCursor: root.cursorActive && root.focusSection === "protection" && root.protectionIndex === 4
                foreground: root.foreground
                fontFamily: root.fontFamily
                onHovered: function(on) { if (on) root.setCursorFromHover("protection", 4) }
                onChanged: function(value) { vpn.setSplitMode(value) }
              }

              // The list is scanned fresh each time the popup opens, so an app
              // installed since the panel loaded is there without a restart.
              // Paths already in Windscribe's file that no longer scan (an app
              // that was removed) are added back as options, otherwise they
              // could never be unticked.
              MultiSelect {
                id: splitAppsRow
                visible: root.splitDetailVisible
                width: parent.width
                label: "Apps"
                options: root.splitStaleApps
                optionsCommand: ["python3", vpn.appsScriptPath]
                placeholderText: "Search apps..."
                emptyText: "No apps found"
                noSelectionText: "None chosen"
                hasCursor: root.cursorActive && root.focusSection === "protection" && root.protectionIndex === 5
                foreground: root.foreground
                fontFamily: root.fontFamily
                onHovered: function(on) { if (on) root.setCursorFromHover("protection", 5) }
                onChanged: function(values) { vpn.setSplitApps(values) }
              }

              // MultiSelect writes to its own `values` when a row is ticked,
              // and that imperative write destroys a plain declarative
              // binding: from the first tick on, the list would show what was
              // clicked rather than what is in the file. Binding reasserts
              // itself every time the service's list changes, so the file
              // stays the one source.
              Binding {
                target: splitAppsRow
                property: "values"
                value: vpn.splitApps
              }

              Text {
                visible: root.splitDetailVisible
                width: parent.width
                text: "Restart each chosen app after connecting, or it keeps using the tunnel it started on."
                textFormat: Text.PlainText
                color: Qt.darker(root.foreground, 1.5)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }

              Toggle {
                width: parent.width
                label: "Allow LAN traffic"
                description: {
                  var raced = vpn.confRacedNote("AllowLANTraffic")
                  if (raced !== "") return raced
                  return vpn.lanTrafficOn ? "Local devices reachable while on the VPN"
                                          : "Local devices are blocked while the firewall is on"
                }
                checked: vpn.lanTrafficOn
                enabled: vpn.splitAvailable && !vpn.busy
                hasCursor: root.cursorActive && root.focusSection === "protection" && root.protectionIndex === root.lanIndex
                foreground: root.foreground
                fontFamily: root.fontFamily
                onHovered: function(on) { if (on) root.setCursorFromHover("protection", root.lanIndex) }
                onClicked: { root.clearHighlight(); vpn.toggleLanTraffic() }
              }

              // ── Account ─────────────────────────────────────────────────
              PanelSectionHeader {
                text: "ACCOUNT"
                foreground: root.foreground
                fontFamily: root.fontFamily
                topPadding: Style.space(10)
              }

              Column {
                width: parent.width
                spacing: Style.spacing.labelGap
                InfoPair { visible: vpn.dataUsage !== ""; label: "Data usage"; value: vpn.dataUsage }
                InfoPair { visible: vpn.dataUsage !== ""; label: "Plan"; value: root.proAccount ? "Pro" : "Free" }
              }

              ActionRow {
                width: parent.width
                hasCursor: root.cursorActive && root.focusSection === "protection" && root.protectionIndex === root.signOutIndex
                icon: "󰍃"
                title: root.signOutArmed ? "Click again to sign out" : "Sign out"
                subtitle: root.signOutArmed ? "Disconnects and logs out on this computer" : "You'll need your password and 2FA to log back in"
                enabled: !vpn.busy
                onEntered: root.setCursorFromHover("protection", root.signOutIndex)
                onClicked: root.requestSignOut()
              }
            }
          }

          // ── Profiles ────────────────────────────────────────────────────
          // Named places in the theme's colours, above Recent. The editor
          // takes the list's place while a profile is being written.
          Column {
            id: profileSection
            visible: vpn.signedIn && root.tab === "connections"
            width: parent.width
            spacing: Style.space(10)

            // While the editor is open the header is only its title.
            FoldHeader {
              section: "profilesHeader"
              title: root.draft === null ? "PROFILES" : (root.draft.isNew ? "NEW PROFILE" : "EDIT PROFILE")
              count: vpn.profiles.length
              expanded: vpn.profilesExpanded
              active: root.draft === null
              onToggled: root.setProfilesExpanded(!vpn.profilesExpanded)
            }

            Column {
              id: profileColumn
              visible: root.draft === null && vpn.profilesExpanded
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: vpn.profiles
                ActionRow {
                  required property var modelData
                  required property int index
                  width: profileColumn.width
                  hasCursor: root.cursorActive && root.focusSection === "profiles" && root.profileIndex === index
                  icon: "\udb81\udf65"
                  iconColor: root.themeColor(modelData.color)
                  title: modelData.name
                  subtitle: [modelData.title, modelData.subtitle].filter(function(v) { return v !== "" }).join(" · ")
                  trailing: vpn.connected && vpn.activeProfile === modelData.id ? "\udb80\udd2c" : ""
                  editable: true
                  enabled: !vpn.busy
                  onEntered: root.setCursorFromHover("profiles", index)
                  onClicked: { vpn.connectProfile(modelData.id); root.showConnection() }
                  onEditClicked: root.openEditor(modelData)
                }
              }

              ActionRow {
                width: profileColumn.width
                hasCursor: root.cursorActive && root.focusSection === "profiles" && root.profileIndex === vpn.profiles.length
                icon: "\udb81\udc15"
                title: "New profile"
                subtitle: vpn.profiles.length === 0 ? "Name a place, pick a colour, get there in one click" : ""
                onEntered: root.setCursorFromHover("profiles", vpn.profiles.length)
                onClicked: root.openEditor(null)
              }
            }

            BorderSurface {
              visible: root.draft !== null
              width: parent.width
              implicitHeight: editorColumn.implicitHeight + Style.space(20)
              radius: Style.cornerRadius
              color: Style.controlFill(false, false, root.foreground, Commons.Color.accent)
              borderSpec: Border.controlSpec("normal", root.foreground, Commons.Color.accent)

              Column {
                id: editorColumn
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(10)
                anchors.rightMargin: Style.space(10)
                spacing: Style.space(8)

                TextField {
                  id: nameField
                  width: parent.width
                  foreground: root.foreground
                  placeholderText: "Name, like Home or Work"
                  maximumLength: vpn.profileNameMax
                  hasCursor: root.cursorActive && root.editorRow === "name"
                  Keys.onEscapePressed: function(event) {
                    keyCatcher.forceActiveFocus()
                    event.accepted = true
                  }
                  Keys.onReturnPressed: function(event) {
                    root.saveDraft()
                    event.accepted = true
                  }
                }

                // One swatch per theme colour; the chosen one grows a ring.
                CursorSurface {
                  id: swatchRow
                  width: parent.width
                  implicitHeight: Style.spacing.controlHeight
                  hasCursor: root.cursorActive && root.editorRow === "color"
                  foreground: root.foreground

                  MouseArea {
                    anchors.fill: parent
                    hoverEnabled: true
                    onEntered: root.setCursorFromHover("editor", 1)
                  }

                  RowLayout {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.leftMargin: Style.spacing.controlPaddingX
                    anchors.rightMargin: Style.spacing.controlPaddingX
                    spacing: Style.space(8)

                    Text {
                      text: "Colour"
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                      Layout.fillWidth: true
                    }

                    Repeater {
                      model: vpn.profileColors
                      Swatch {
                        required property var modelData
                        colorName: modelData
                      }
                    }
                  }
                }

                SearchableDropdown {
                  id: whereRow
                  width: parent.width
                  label: "Where"
                  value: root.draft !== null ? root.draft.where : ""
                  options: root.whereOptions
                  placeholderText: "Search places..."
                  emptyText: "No places match"
                  hasCursor: root.cursorActive && root.editorRow === "where"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  onHovered: function(on) { if (on) root.setCursorFromHover("editor", 2) }
                  onChanged: function(v) { root.setDraft("where", v) }
                }

                // The dropdown writes its own `value` when a row is picked,
                // which destroys a plain binding; this puts the draft back
                // in charge, so reopening the editor shows the profile's real
                // place and not the last thing clicked.
                Binding { target: whereRow; property: "value"; value: root.draft !== null ? root.draft.where : "" }

                Row {
                  spacing: Style.space(8)

                  Button {
                    text: "Save"
                    bordered: true
                    foreground: root.foreground
                    hasCursor: root.cursorActive && root.editorButton === "save"
                    onHovered: function(on) { if (on) root.hoverEditorButton(0) }
                    onClicked: root.saveDraft()
                  }

                  Button {
                    text: "Cancel"
                    foreground: root.dim
                    hasCursor: root.cursorActive && root.editorButton === "cancel"
                    onHovered: function(on) { if (on) root.hoverEditorButton(1) }
                    onClicked: root.closeEditor(root.draft !== null ? root.draft.id : "")
                  }

                  Button {
                    visible: root.draft !== null && !root.draft.isNew
                    text: "Delete"
                    foreground: root.urgent
                    hasCursor: root.cursorActive && root.editorButton === "delete"
                    onHovered: function(on) { if (on) root.hoverEditorButton(2) }
                    onClicked: root.deleteDraft()
                  }
                }
              }
            }
          }

          // ── Recent ──────────────────────────────────────────────────────
          Column {
            id: recentSection
            visible: vpn.signedIn && root.tab === "connections" && vpn.recents.length > 0
            width: parent.width
            spacing: Style.space(10)

            FoldHeader {
              section: "recentsHeader"
              title: "RECENT"
              count: vpn.recents.length
              expanded: vpn.recentsExpanded
              onToggled: root.setRecentsExpanded(!vpn.recentsExpanded)
            }

            Column {
              id: recentColumn
              visible: vpn.recentsExpanded
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: vpn.recents
                ActionRow {
                  required property var modelData
                  required property int index
                  width: recentColumn.width
                  hasCursor: root.cursorActive && root.focusSection === "recents" && root.recentIndex === index
                  title: modelData.title || ""
                  subtitle: modelData.subtitle || ""
                  enabled: !vpn.busy
                  onEntered: root.setCursorFromHover("recents", index)
                  onClicked: { vpn.connectRecent(index); root.showConnection() }
                }
              }
            }
          }

          // ── Regions and cities ──────────────────────────────────────────
          Column {
            id: regionSection
            visible: vpn.signedIn && root.tab === "connections"
            width: parent.width
            spacing: Style.space(10)

            // Inside a drill the header is only the region's name, with
            // nothing to click.
            //
            // Choosing a location is a Pro feature: on Free the CLI refuses
            // every connect that names a place. So the section wears PRO on
            // a free account, the same way the account section shows the
            // plan; on Pro the tag is gone.
            FoldHeader {
              section: "regionsHeader"
              title: root.drilled ? String(root.drilledName).toUpperCase() : "REGIONS"
              count: vpn.regionsLoaded ? vpn.regions.length : -1
              tag: root.proAccount ? "" : "PRO"
              expanded: vpn.countriesExpanded
              active: !root.drilled
              onToggled: { root.clearHighlight(); root.setCountriesExpanded(!vpn.countriesExpanded) }
            }

            BackRow {
              visible: root.drilled
              width: parent.width
            }

            Column {
              id: cityColumn
              visible: root.drilled && !!root.drilledRegion
              width: parent.width
              spacing: Style.space(6)

              FastestRow { width: cityColumn.width }

              Repeater {
                model: root.drilledRegion ? root.drilledRegion.cities : []
                CityRow {
                  required property var modelData
                  required property int index
                  width: cityColumn.width
                  city: modelData
                  // Row 0 is "Fastest in <region>", so cities start at 1.
                  rowIndex: index + 1
                }
              }
            }

            TextField {
              id: filterField
              visible: !root.drilled && vpn.countriesExpanded
              width: parent.width
              foreground: root.foreground
              placeholderText: vpn.regionsLoaded ? "Filter regions  (press /)" : "Loading locations…"
              enabled: vpn.regionsLoaded
              text: root.filterQuery
              onTextChanged: {
                root.filterQuery = text
                root.regionIndex = 0
              }
              Keys.onEscapePressed: function(event) {
                if (text !== "") text = ""
                keyCatcher.forceActiveFocus()
                event.accepted = true
              }
              Keys.onReturnPressed: function(event) {
                var r = root.filteredRegions[0]
                if (r) {
                  root.drillInto(r.name)
                  keyCatcher.forceActiveFocus()
                }
                event.accepted = true
              }
            }

            Text {
              visible: !root.drilled && vpn.countriesExpanded && vpn.regionsLoaded && root.filteredRegions.length === 0
              width: parent.width
              text: "No regions match."
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              horizontalAlignment: Text.AlignHCenter
            }

            Column {
              id: regionColumn
              visible: !root.drilled && vpn.countriesExpanded
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: root.filteredRegions
                RegionRow {
                  required property var modelData
                  required property int index
                  width: regionColumn.width
                  region: modelData
                  rowIndex: index
                }
              }
            }
          }
        }
      }
    }
  }

  // A clickable row: optional icon, title + subtitle, optional trailing tag.
  // Used for install, sign-in, quick connect, static IPs, recents and the
  // protection actions so they all read the same way.
  // A section header that is a row of its own: it folds the rows under it
  // away and unfolds them, carries their count, and a chevron says which way
  // it goes. It hangs out past the column by a row's inset so its title
  // stays in line with the plain section headers while its cursor ring reads
  // like every other row's. With `active` off it is only a title.
  component FoldHeader: CursorSurface {
    id: foldHeader
    property string section: ""
    property string title: ""
    // Below zero hides the count, for a list that has not loaded yet.
    property int count: -1
    // A tag after the title and count, the way the regions header wears
    // PRO on a free account. "" for none.
    property string tag: ""
    property bool expanded: true
    property bool active: true
    signal toggled()

    x: -Style.space(10)
    width: parent.width + Style.space(20)
    hasCursor: active && root.cursorActive && root.focusSection === section
    foreground: root.foreground
    implicitHeight: foldLabel.implicitHeight + Style.space(10)

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      enabled: foldHeader.active
      cursorShape: Qt.PointingHandCursor
      onEntered: root.setCursorFromHover(foldHeader.section, 0)
      onClicked: foldHeader.toggled()
    }

    PanelSectionHeader {
      id: foldLabel
      anchors.left: parent.left
      anchors.leftMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      text: foldHeader.title
      textFormat: Text.PlainText
      foreground: root.foreground
      fontFamily: root.fontFamily
    }

    Text {
      id: foldCount
      anchors.left: foldLabel.right
      anchors.leftMargin: Style.space(8)
      anchors.verticalCenter: foldLabel.verticalCenter
      visible: foldHeader.active && foldHeader.count >= 0
      text: String(foldHeader.count)
      textFormat: Text.PlainText
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      anchors.left: foldCount.visible ? foldCount.right : foldLabel.right
      anchors.leftMargin: Style.space(8)
      anchors.verticalCenter: foldLabel.verticalCenter
      visible: foldHeader.tag !== ""
      text: foldHeader.tag
      textFormat: Text.PlainText
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      anchors.right: parent.right
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: foldLabel.verticalCenter
      visible: foldHeader.active
      text: foldHeader.expanded ? "\udb80\udd40" : "\udb80\udd42"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }
  }

  component ActionRow: CursorSurface {
    id: actionRow
    property string icon: ""
    property color iconColor: root.foreground
    property string title: ""
    property string subtitle: ""
    property string trailing: ""
    // A pencil on the right, and a right-click anywhere, open the editor.
    property bool editable: false
    signal entered()
    signal clicked()
    signal editClicked()

    foreground: root.foreground
    implicitHeight: actionContent.implicitHeight + Style.spacing.rowPaddingX
    opacity: enabled ? 1.0 : 0.6

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: actionRow.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
      enabled: actionRow.enabled
      acceptedButtons: actionRow.editable ? (Qt.LeftButton | Qt.RightButton) : Qt.LeftButton
      onEntered: actionRow.entered()
      onClicked: function(mouse) {
        root.clearHighlight()
        if (actionRow.editable && mouse.button === Qt.RightButton) actionRow.editClicked()
        else actionRow.clicked()
      }
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      Text {
        visible: actionRow.icon !== ""
        text: actionRow.icon
        color: actionRow.iconColor
        font.family: root.fontFamily
        font.pixelSize: Style.font.heading
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        id: actionContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          Layout.fillWidth: true
          text: actionRow.title
          textFormat: Text.PlainText
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          Layout.fillWidth: true
          visible: actionRow.subtitle !== ""
          text: actionRow.subtitle
          textFormat: Text.PlainText
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      Text {
        visible: actionRow.trailing !== ""
        text: actionRow.trailing
        textFormat: Text.PlainText
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        Layout.alignment: Qt.AlignVCenter
      }

      Text {
        visible: actionRow.editable
        text: "\udb80\udfeb"
        color: editHover.containsMouse ? root.foreground : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        Layout.alignment: Qt.AlignVCenter
        leftPadding: Style.space(4)

        MouseArea {
          id: editHover
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: { root.clearHighlight(); actionRow.editClicked() }
        }
      }
    }
  }

  // One theme colour as a dot; the chosen one is ringed in the foreground.
  component Swatch: Item {
    id: swatch
    property string colorName: ""
    readonly property bool selected: root.draft !== null && root.draft.color === colorName
    implicitWidth: Style.space(22)
    implicitHeight: Style.space(22)
    Layout.alignment: Qt.AlignVCenter

    Rectangle {
      anchors.centerIn: parent
      width: swatch.selected ? Style.space(20) : Style.space(14)
      height: width
      radius: width / 2
      color: root.themeColor(swatch.colorName)
      border.width: swatch.selected ? 2 : 0
      border.color: root.foreground
      Behavior on width { NumberAnimation { duration: 120 } }
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: root.setCursorFromHover("editor", 1)
      onClicked: root.setDraft("color", swatch.colorName)
    }
  }

  component TabPill: Button {
    id: pill
    property string tabKey: ""

    fontSize: Style.font.bodySmall
    foreground: root.foreground
    fontFamily: root.fontFamily
    horizontalPadding: Style.spacing.controlPaddingX
    verticalPadding: Style.spacing.controlPaddingY + Style.space(2)
    bordered: true
    active: root.tab === tabKey
    // The ring sits on the tab that is showing: the row is one cursor stop
    // and h and l move it.
    hasCursor: root.cursorActive && root.focusSection === "tabs" && root.tab === tabKey

    onHovered: function(isHovered) { if (isHovered) root.setCursorFromHover("tabs", 0) }
    onClicked: root.setTab(tabKey)   // setTab clears the highlight
  }

  component RegionRow: CursorSurface {
    id: regionRow
    property var region: null
    property int rowIndex: 0

    // The row stands for a whole region, so any of its cities counts as
    // current. The status names the city, the region's cities say whether
    // it belongs here.
    readonly property bool isCurrent: {
      if (!vpn.connected || !region) return false
      var city = String(vpn.currentCity).toLowerCase()
      for (var i = 0; i < region.cities.length; i++) {
        if (String(region.cities[i].name).toLowerCase() === city) return true
      }
      return false
    }

    hasCursor: root.cursorActive && root.focusSection === "regions" && root.regionIndex === rowIndex
    foreground: root.foreground

    implicitHeight: regionContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: vpn.busy ? Qt.ArrowCursor : Qt.PointingHandCursor
      enabled: !vpn.busy
      onEntered: root.setCursorFromHover("regions", regionRow.rowIndex)
      onClicked: { root.clearHighlight(); root.drillInto(regionRow.region ? regionRow.region.name : "") }
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      ColumnLayout {
        id: regionContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          Layout.fillWidth: true
          text: regionRow.region ? regionRow.region.name : ""
          textFormat: Text.PlainText
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }
      }

      Text {
        text: regionRow.isCurrent ? "\udb80\udd2c" : (regionRow.region ? regionRow.region.cities.length : "")
        textFormat: Text.PlainText
        color: regionRow.isCurrent ? root.foreground : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        Layout.alignment: Qt.AlignVCenter
      }

      // Signals that the row opens a city list rather than connecting.
      Text {
        text: "›"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        Layout.alignment: Qt.AlignVCenter
      }
    }
  }

  component BackRow: CursorSurface {
    id: backRow
    foreground: root.foreground
    implicitHeight: backLabel.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.drillOut()
    }

    Text {
      id: backLabel
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      text: "‹  All regions"
      textFormat: Text.PlainText
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }

  // Row 0 of a drill: keeps the one-click "connect anywhere in the region"
  // path. `windscribe-cli connect <region>` picks a random datacenter in
  // it, which is the closest thing to "fastest in the region" there is.
  component FastestRow: CursorSurface {
    id: fastestRow
    hasCursor: root.cursorActive && root.focusSection === "cities" && root.cityIndex === 0
    foreground: root.foreground
    implicitHeight: fastestContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: vpn.busy ? Qt.ArrowCursor : Qt.PointingHandCursor
      enabled: !vpn.busy
      onEntered: root.setCursorFromHover("cities", 0)
      onClicked: { root.clearHighlight(); root.activateCityRow(0) }
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      ColumnLayout {
        id: fastestContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          Layout.fillWidth: true
          text: "Fastest in " + root.drilledName
          textFormat: Text.PlainText
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          Layout.fillWidth: true
          text: root.drilledRegion ? "Random datacenter in the region" : "Let the engine choose"
          textFormat: Text.PlainText
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }

  component CityRow: CursorSurface {
    id: cityRow
    property var city: null
    property int rowIndex: 0

    readonly property bool isCurrent: vpn.connected && city
                                      && String(vpn.currentCity).toLowerCase() === String(city.name).toLowerCase()

    hasCursor: root.cursorActive && root.focusSection === "cities" && root.cityIndex === rowIndex
    foreground: root.foreground
    implicitHeight: cityContent.implicitHeight + Style.spacing.rowPaddingX

    // The city that was clicked on the map breathes until the next click.
    readonly property bool pulsing: root.highlight !== null && city
                                    && root.highlight.region === root.drilledName
                                    && root.highlight.city === city.name
    Rectangle {
      anchors.fill: parent
      radius: Style.cornerRadius
      color: "transparent"
      border.color: root.foreground
      border.width: 1.5
      visible: cityRow.pulsing
      SequentialAnimation on opacity {
        running: cityRow.pulsing
        loops: Animation.Infinite
        NumberAnimation { from: 0.25; to: 0.95; duration: 700; easing.type: Easing.InOutSine }
        NumberAnimation { from: 0.95; to: 0.25; duration: 700; easing.type: Easing.InOutSine }
      }
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: (vpn.busy || cityRow.city.disabled) ? Qt.ArrowCursor : Qt.PointingHandCursor
      enabled: !vpn.busy && cityRow.city && !cityRow.city.disabled
      onEntered: root.setCursorFromHover("cities", cityRow.rowIndex)
      onClicked: { root.clearHighlight(); if (cityRow.city) root.activateCityRow(cityRow.rowIndex) }
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      ColumnLayout {
        id: cityContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          Layout.fillWidth: true
          text: cityRow.city ? cityRow.city.name : ""
          textFormat: Text.PlainText
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          Layout.fillWidth: true
          visible: cityRow.city && (cityRow.city.nicknames.length > 0 || cityRow.city.disabled)
          textFormat: Text.PlainText
          text: {
            if (!cityRow.city) return ""
            if (cityRow.city.disabled) return "Temporarily unavailable"
            var bits = cityRow.city.nicknames.slice(0, 3)
            if (cityRow.city.tenGbps) bits.push("10 Gbps")
            return bits.join(" · ")
          }
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      Text {
        text: cityRow.isCurrent ? "\udb80\udd2c" : (cityRow.city && cityRow.city.pro ? "PRO" : "")
        textFormat: Text.PlainText
        color: cityRow.isCurrent ? root.foreground : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        Layout.alignment: Qt.AlignVCenter
      }
    }
  }

  component InfoPair: Row {
    property string label: ""
    property string value: ""

    width: parent.width
    spacing: Style.space(8)

    InfoLabel { text: label }
    Item {
      width: Math.max(0, parent.width - parent.children[0].implicitWidth - parent.children[2].implicitWidth - parent.spacing * 2)
      height: 1
    }
    InfoValue { text: value }
  }

  component InfoLabel: Text {
    color: root.foreground
    opacity: 0.6
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  component InfoValue: Text {
    color: root.foreground
    textFormat: Text.PlainText
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
    elide: Text.ElideRight
  }
}
