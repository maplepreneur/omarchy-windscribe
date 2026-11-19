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
// always-on firewall mode, so the widget edits those keys itself. The
// writing is done by the bundled confedit.py: it reads the file fresh at
// the moment of the edit (the engine rewrites the same file, so a cached
// copy could revert a newer change), changes exactly one key's line, and
// reads the key back to confirm what actually landed. What remains here
// is the reading side, and the string-list encoding for the app list.

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

// ── QSettings-compatible string lists ───────────────────────────────────
//
// The engine is a Qt app, and the app list is stored as a QSettings INI
// string list. Qt's rules are not "split on commas": an element is
// escaped (C-style for quotes, backslashes and control characters) and
// quoted in double quotes when it contains `;`, `,` or `=`, or leads or
// trails with a space; the elements are joined with ", "; reading
// splits on commas outside quotes, honours the escapes, skips spaces
// after each comma, and chops trailing spaces from unquoted elements.
// A path with a comma in it is one entry, not two.
//
// Both functions below are ports of Qt's iniEscapedStringList /
// iniUnescapedStringList (qsettings.cpp), kept close enough that
// anything this widget writes reads back identically in the engine and
// anything the engine writes reads back identically here.

// Qt's iniEscapedString: one element, escaped and quoted when needed.
function encodeIniString(str) {
  var s = String(str)
  var out = ""
  var quoted = s.indexOf(";") !== -1 || s.indexOf(",") !== -1 || s.indexOf("=") !== -1
  var escapeNextIfDigit = false
  for (var i = 0; i < s.length; i++) {
    var code = s.charCodeAt(i)
    var ch = s.charAt(i)
    // A hex escape followed by a hex digit would be read as one longer
    // escape, so the digit gets its own. Qt does the same, which is
    // what makes the pair round-trip.
    if (escapeNextIfDigit && /[0-9a-fA-F]/.test(ch)) {
      out += "\\x" + code.toString(16)
      continue
    }
    escapeNextIfDigit = false
    switch (ch) {
      case "\u0000": out += "\\0"; escapeNextIfDigit = true; break
      case "\u0007": out += "\\a"; break
      case "\u0008": out += "\\b"; break
      case "\u000c": out += "\\f"; break
      case "\u000a": out += "\\n"; break
      case "\u000d": out += "\\r"; break
      case "\u0009": out += "\\t"; break
      case "\u000b": out += "\\v"; break
      case '"': out += '\\"'; break
      case "\\": out += "\\\\"; break
      default:
        if (code <= 0x1F || code === 0x7F) {
          out += "\\x" + code.toString(16)
          escapeNextIfDigit = true
        } else {
          out += ch
        }
    }
  }
  if (quoted || (out.length > 0 && (out.charAt(0) === " " || out.charAt(out.length - 1) === " ")))
    out = '"' + out + '"'
  return out
}

// Qt's iniEscapedStringList, with one deliberate difference: Qt writes an
// empty list as "@Invalid()"; this widget writes an empty value, which is
// what the engine itself leaves in a fresh settings file and what this
// widget has always written for "no apps chosen", so an empty selection
// never changes representation under the engine.
function encodeIniStringList(arr) {
  var src = (arr && typeof arr.length === "number") ? arr : []
  var out = []
  for (var i = 0; i < src.length; i++) out.push(encodeIniString(src[i]))
  return out.join(", ")
}

// Qt's iniUnescapedStringList: the value of one INI key as the engine
// itself would split it. With no unquoted comma the whole value is one
// element, exactly as Qt returns it as a plain string.
function parseIniStringList(raw) {
  var str = String(raw || "")
  var list = null            // null until the first unquoted comma
  var cur = ""
  var inQuotes = false
  var quoted = false         // the current element had a quote: keep its spaces
  var chop = 0               // spaces after this point are trailing, and go
  var i = 0
  var n = str.length
  var state = 0              // 0 normal, 1 hex escape, 2 octal escape
  var escapeVal = 0
  var done = false
  var skipFinalChop = false  // Qt jumps past the final chop from its escape states

  var SINGLE = { a: "\u0007", b: "\u0008", f: "\u000c", n: "\u000a",
                r: "\u000d", t: "\u0009", v: "\u000b",
                '"': '"', "?": "?", "'": "'", "\\": "\\" }
  function hexVal(c) { return /[0-9a-fA-F]/.test(c) ? parseInt(c, 16) : -1 }
  function octVal(c) { return c >= "0" && c <= "7" ? c.charCodeAt(0) - 48 : -1 }
  function skipSpaces() {
    while (i < n && (str.charAt(i) === " " || str.charAt(i) === "\t")) i++
  }
  function chopSpaces() {
    while (cur.length - 1 >= chop && (cur.charAt(cur.length - 1) === " " || cur.charAt(cur.length - 1) === "\t"))
      cur = cur.slice(0, cur.length - 1)
  }

  skipSpaces()
  while (!done) {
    if (state === 0) {
      chop = cur.length
      while (i < n) {
        var c = str.charAt(i)
        if (c === "\\") {
          i++
          if (i >= n) { done = true; skipFinalChop = true; break }
          var e = str.charAt(i)
          i++
          if (SINGLE[e] !== undefined) { cur += SINGLE[e]; chop = cur.length; continue }
          if (e === "x") {
            escapeVal = 0
            if (i >= n) { done = true; skipFinalChop = true; break }
            if (hexVal(str.charAt(i)) !== -1) { state = 1; break }
            // "\x" then something else: both are dropped, the something
            // else is not consumed.
            chop = cur.length
            continue
          }
          var o = octVal(e)
          if (o !== -1) { escapeVal = o; state = 2; break }
          if (e === "\n" || e === "\r") {
            // A line-continuation pair, not content.
            if (i < n) {
              var c2 = str.charAt(i)
              if ((c2 === "\n" || c2 === "\r") && c2 !== e) i++
            }
          }
          // An escape Qt doesn't recognise: the backslash and the
          // character after it are both dropped.
          chop = cur.length
          continue
        }
        if (c === '"') {
          i++
          quoted = true
          inQuotes = !inQuotes
          if (!inQuotes) { skipSpaces(); chop = cur.length }
          continue
        }
        if (c === "," && !inQuotes) {
          if (!quoted) chopSpaces()
          if (list === null) list = []
          list.push(cur)
          cur = ""
          quoted = false
          i++
          skipSpaces()
          chop = cur.length
          continue
        }
        cur += c
        i++
      }
      if (i >= n) {
        done = true
        if (!skipFinalChop && !quoted) chopSpaces()
      }
      continue
    }
    if (state === 1) {
      if (i >= n) { cur += String.fromCharCode(escapeVal); done = true; skipFinalChop = true; continue }
      var h = hexVal(str.charAt(i))
      if (h !== -1) { escapeVal = (escapeVal << 4) + h; i++; continue }
      cur += String.fromCharCode(escapeVal)
      state = 0
      continue
    }
    // state === 2
    if (i >= n) { cur += String.fromCharCode(escapeVal); done = true; skipFinalChop = true; continue }
    var ov = octVal(str.charAt(i))
    if (ov !== -1) { escapeVal = (escapeVal << 3) + ov; i++; continue }
    cur += String.fromCharCode(escapeVal)
    state = 0
  }
  if (list === null) return [cur]
  list.push(cur)
  return list
}
