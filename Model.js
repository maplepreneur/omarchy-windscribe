// Parsing helpers for the Windscribe CLI's human-readable output.
//
// The CLI has no --json anywhere, so every parser here is deliberately
// tolerant: unknown lines are skipped rather than treated as errors, and
// the detail rows are rendered from whatever "Label: value" pairs
// `windscribe-cli status` happens to print. A future CLI release that adds
// or renames a field shows up as a new row instead of a broken widget.

function stripAnsi(text) {
  return String(text || "").replace(/\x1b\[[0-9;?]*[a-zA-Z]/g, "")
}

// "Label: value" lines -> ordered list plus a lowercased lookup map.
// Guards against prose (long labels, column-aligned table rows) being
// mistaken for fields.
function parseKeyValues(raw) {
  var out = { order: [], map: {} }
  var lines = stripAnsi(raw).split("\n")
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim()
    if (line === "") continue
    var idx = line.indexOf(":")
    if (idx <= 0) continue
    var label = line.substring(0, idx).trim()
    var value = line.substring(idx + 1).trim()
    if (label === "" || value === "") continue
    if (label.length > 28) continue
    if (/\s{2,}/.test(label)) continue
    out.order.push({ label: label, value: value })
    out.map[label.toLowerCase()] = value
  }
  return out
}

// `windscribe-cli status`
//
//   Login:       Logged in            (or Logged out / Logging in / Error: <reason>)
//   Connection:  Connected to Toronto - Comfort Zone
//                ("(checking the connection)" or "(network interference detected)"
//                 can follow; Connecting to <city> / Disconnecting / Disconnected
//                 / Error: <reason>)
//   Protocol:    WireGuard:443        (when connected)
//   IP:          149.88.98.88 (VPN)   (or <ip> (public); line can be absent)
//   Firewall:    On | Off | Always On
//   Data usage:  39.97 MB / Unlimited (when logged in)
//   Internet:    Available | Unavailable
//   Update:      2.26.0 available     (only when there is one)
//
// The Connection line is only printed while logged in; while logged out
// the status is just "logged out", whatever the tunnel does.
function parseStatus(raw) {
  var kv = parseKeyValues(raw)
  var login = String(kv.map["login"] || "")
  var connection = String(kv.map["connection"] || "")

  var signedIn = /^logged in$/i.test(login)
  var loginError = login.indexOf("Error:") === 0 ? login.replace(/^Error:\s*/i, "").trim() : ""

  // "Connected to Toronto - Comfort Zone (network interference detected)"
  // -> where "Toronto - Comfort Zone", checking true/false, interference true
  var m = connection.match(/^connected to\s+(.+?)(\s+\((checking the connection|network interference detected)\))?$/i)
  var connected = !!m
  var connecting = /^connecting to/i.test(connection)
  var disconnecting = /^disconnecting$/i.test(connection)
  var statusText = connection !== "" ? connection : (login !== "" ? login : "Unknown")
  var errorText = connection.indexOf("Error:") === 0 ? connection.replace(/^Error:\s*/i, "").trim() : ""
  var where = m ? m[1] : ""
  var checking = m && m[2] !== undefined && /checking/i.test(m[2])
  var interference = m && m[2] !== undefined && /interference/i.test(m[2])

  // Fields that already headline the hero or have their own rows are
  // kept out of the generic detail list.
  var fields = []
  for (var i = 0; i < kv.order.length; i++) {
    var key = kv.order[i].label.toLowerCase()
    if (key === "login" || key === "connection") continue
    fields.push(kv.order[i])
  }

  return {
    ok: true,
    signedIn: signedIn,
    loginError: loginError,
    connected: connected,
    connecting: connecting,
    disconnecting: disconnecting,
    errorText: errorText,
    statusText: statusText,
    where: where,
    checking: checking,
    interference: interference,
    protocol: String(kv.map["protocol"] || ""),
    ip: String(kv.map["ip"] || ""),
    firewall: String(kv.map["firewall"] || ""),
    dataUsage: String(kv.map["data usage"] || ""),
    internet: String(kv.map["internet"] || ""),
    update: String(kv.map["update"] || ""),
    fields: fields
  }
}

// True when `raw` carries a whole answer from `windscribe-cli status`:
// a Login line whose value is one of the words the CLI prints. Tells a run
// that answered and then died on its way out from one that never answered.
function statusComplete(raw) {
  var v = parseKeyValues(raw).map["login"] || ""
  return /^(logged in|logged out|logging in|error:.*)$/i.test(v)
}

// "Toronto - Comfort Zone" -> { city: "Toronto", nickname: "Comfort Zone" }
// The connection line names the city and the datacenter nickname; the
// city is what the bundled coordinates know.
function splitPlace(where) {
  var w = String(where || "").trim()
  var parts = w.split(" - ")
  return {
    city: parts[0] !== undefined ? parts[0].trim() : w,
    nickname: parts.length > 1 ? parts.slice(1).join(" - ").trim() : ""
  }
}

// Notification bodies are rendered as markup by the shell, so text that
// came from the CLI is escaped before it goes into one.
function escapeMarkup(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
}

// `windscribe-cli locations` prints one location per line:
//
//   Region - City - Nickname
//   US East - New York - Big Apple (10 Gbps)
//   Best Location - Comfort Zone (10 Gbps)
//
// A line ends with (Pro) if it needs Windscribe Pro, (Disabled) if it is
// temporarily unavailable, and (10 Gbps) on 10 Gbps servers; flags can
// combine. The Best Location line has no nickname.
function parseLocations(raw) {
  var lines = stripAnsi(raw).split("\n")
  var out = []
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim()
    if (line === "") continue
    var flags = line.match(/\((10 Gbps|Pro|Disabled)\)(?=\s|\(|$)/gi) || []
    line = line.replace(/\s*\((10 Gbps|Pro|Disabled)\)(?=\s|\(|$)/gi, "").trim()
    if (line === "") continue
    var parts = line.split(" - ")
    if (parts.length < 2) continue
    var region = parts[0].trim()
    var city = parts[1].trim()
    var nickname = parts.length > 2 ? parts.slice(2).join(" - ").trim() : ""
    if (region === "" || city === "") continue
    var f = {}
    for (var j = 0; j < flags.length; j++) {
      // "(10 Gbps)" -> "10 gbps": key by the words, not the parens.
      f[String(flags[j]).replace(/[()]/g, "").toLowerCase()] = true
    }
    out.push({
      region: region,
      city: city,
      nickname: nickname,
      tenGbps: !!f["10 gbps"],
      pro: !!f["pro"],
      disabled: !!f["disabled"]
    })
  }
  return out
}

// Collapse a flat location list into regions with their cities, keeping
// the CLI's own order: Best Location first, then as printed. A city row
// carries its nicknames joined for the subtitle and flags from any of its
// datacenters; connect takes the city name, which picks a random
// datacenter in it.
function groupLocations(list) {
  var regions = []
  var byName = {}
  for (var i = 0; i < list.length; i++) {
    var loc = list[i]
    var region = byName[loc.region]
    if (!region) {
      region = { name: loc.region, cities: [] }
      byName[loc.region] = region
      regions.push(region)
    }
    var city = null
    for (var j = 0; j < region.cities.length; j++) {
      if (region.cities[j].name === loc.city) { city = region.cities[j]; break }
    }
    if (!city) {
      city = { name: loc.city, nicknames: [], tenGbps: false, pro: false, disabled: false }
      region.cities.push(city)
    }
    if (loc.nickname !== "" && city.nicknames.indexOf(loc.nickname) === -1) city.nicknames.push(loc.nickname)
    if (loc.tenGbps) city.tenGbps = true
    if (loc.pro) city.pro = true
    if (loc.disabled) city.disabled = true
  }
  return regions
}

// `windscribe-cli locations static`
//
//   Device name: Linux 1
//   Toronto - 206.223.183.218
//
// The device-name header names this machine's Windscribe device slot; the
// rows are one static IP each, by city and IP.
function parseStatics(raw) {
  var lines = stripAnsi(raw).split("\n")
  var out = []
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim()
    if (line === "") continue
    if (/^device name:/i.test(line)) continue
    var parts = line.split(" - ")
    if (parts.length !== 2) continue
    var city = parts[0].trim()
    var ip = parts[1].trim()
    if (city === "" || !/^(\d{1,3}\.){3}\d{1,3}$/.test(ip)) continue
    out.push({ city: city, ip: ip })
  }
  return out
}

// `windscribe-cli connect` prints:
//
//   Connecting to Toronto - Comfort Zone
//   Connected to Toronto - Comfort Zone (checking the connection)
//   Connected to Toronto - Comfort Zone
//
// The final "Connected to <place>" line is what names where the request
// landed; searched for, never assumed to be the last line.
function connectedLine(raw) {
  var lines = stripAnsi(raw).split("\n")
  var found = ""
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim()
    if (/^connected to\s+\S/i.test(line)) found = line
  }
  return found
}

// That line is the only place a "best" connect names the location it
// actually landed on, so it's what lets Best and Last-used show up in
// Recent alongside places picked by hand.
function parseConnected(raw) {
  var line = connectedLine(raw)
  if (line === "") return null
  var m = line.match(/connected to\s+(.+?)\s*\.?\s*$/i)
  if (!m) return null
  var place = splitPlace(m[1])
  if (place.city === "") return null
  return place
}

function filterRegions(regions, query) {
  var q = String(query || "").trim().toLowerCase()
  if (q === "") return regions
  var out = []
  for (var i = 0; i < regions.length; i++) {
    var r = regions[i]
    if (r.name.toLowerCase().indexOf(q) !== -1) { out.push(r); continue }
    // A region whose city matches still shows, with just that city.
    var cities = []
    for (var j = 0; j < r.cities.length; j++) {
      if (r.cities[j].name.toLowerCase().indexOf(q) !== -1) cities.push(r.cities[j])
    }
    if (cities.length > 0) out.push({ name: r.name, cities: cities })
  }
  return out
}

function elide(text, max) {
  var value = String(text || "").replace(/\s+/g, " ").trim()
  var limit = max || 140
  return value.length > limit ? value.substring(0, limit - 3) + "…" : value
}

// Windscribe usernames are letters, numbers and . _ -, or the email
// address the account was made with. Anything outside this set is refused
// outright rather than escaped: it has no business in a username, and
// refusing is simpler to reason about than quoting.
function validUsername(text) {
  return /^[A-Za-z0-9._@+-]{1,254}$/.test(String(text || "").trim())
}

// Single-quote for POSIX sh. The terminal launcher joins its arguments
// into one `bash -c` string, so this is the only path where a value has
// to cross a shell boundary; validUsername() already narrowed it to a safe
// alphabet.
function shellQuote(text) {
  return "'" + String(text).replace(/'/g, "'\\''") + "'"
}

// ── Windscribe's preferences file ────────────────────────────────────────
//
// ~/.config/Windscribe/windscribe_cli.conf is an INI file the engine
// watches and applies "as soon as the file is saved" (its own help text).
// The CLI itself has no commands for split tunneling, LAN traffic or the
// always-on firewall mode, so the widget edits those keys here. The rules
// are the same ones OmaProton follows with Proton's settings.json: read it
// fresh, change one key's value, hand every other byte back exactly as
// found, and never create the file.

// The whole parsed [section] -> { key: value }, or null when the section
// isn't there. A key with no value ("Key=") is an empty string, which is
// a real value, not a missing one.
function parseConfSection(raw, section) {
  var lines = String(raw || "").split("\n")
  var inSection = false
  var found = false
  var out = {}
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i]
    var trimmed = line.trim()
    if (trimmed.charAt(0) === "[") {
      if (found) break
      inSection = trimmed === "[" + section + "]"
      continue
    }
    if (!inSection) continue
    var eq = trimmed.indexOf("=")
    if (eq <= 0) continue
    var key = trimmed.substring(0, eq).trim()
    var value = trimmed.substring(eq + 1).trim()
    if (key === "") continue
    out[key] = value
    found = true
  }
  return found ? out : null
}

// Rewrite one key's value in one section, changing nothing else: every
// line outside that key is handed back byte-identical, including section
// order, unknown keys, blank lines and spacing. Returns "" when the key
// or section isn't there, because inventing a home for a setting the
// engine didn't write is guessing, not editing.
function setConfValue(raw, section, key, value) {
  var lines = String(raw || "").split("\n")
  var inSection = false
  var done = false
  var out = []
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i]
    var trimmed = line.trim()
    if (trimmed.charAt(0) === "[") {
      if (inSection && !done) { inSection = false; break }
      inSection = trimmed === "[" + section + "]"
      out.push(line)
      continue
    }
    if (inSection && !done) {
      var eq = trimmed.indexOf("=")
      var name = eq > 0 ? trimmed.substring(0, eq).trim() : ""
      if (name === key) {
        var indent = line.substring(0, line.indexOf(name)) // keep left padding
        out.push((indent || "") + key + "=" + String(value))
        done = true
        continue
      }
    }
    out.push(line)
  }
  if (!done) return ""
  // Keep the file's own trailing-newline habit: a file that ended with a
  // newline still does, one that didn't still doesn't.
  var text = out.join("\n")
  if (/[\r\n]$/.test(String(raw || ""))) text += "\n"
  return text
}
