// Parser tests for Model.js, run with: node tests/model-test.mjs
// Every CLI fixture is real output captured from windscribe-cli 2.25.8.
// The settings-file tests cover the string-list encoding the engine's
// own Qt parser uses (ported in Model.js, verified against real QSettings)
// and the bundled confedit.py writer.
import { readFileSync, writeFileSync, mkdtempSync, statSync, existsSync } from "node:fs"
import { spawnSync } from "node:child_process"
import { tmpdir } from "node:os"
import { fileURLToPath } from "node:url"
import { dirname, join } from "node:path"

const dir = dirname(fileURLToPath(import.meta.url))
globalThis.dir = dir
const source = readFileSync(join(dir, "..", "Model.js"), "utf8")
// Model.js is a plain script; evaluate it in this scope.
const Model = new Function(source + "\n; return { stripAnsi, parseKeyValues, parseStatus, statusComplete, splitPlace, escapeMarkup, parseLocations, groupLocations, parseStatics, connectedLine, parseConnected, filterRegions, elide, validUsername, shellQuote, parseConfSection, encodeIniString, encodeIniStringList, parseIniStringList };")()

let pass = 0, fail = 0
function ok(name, cond, detail) {
  if (cond) { pass++; console.log("  ok - " + name) }
  else { fail++; console.log("  FAIL - " + name + (detail ? ": " + detail : "")) }
}
function eq(name, actual, expected) {
  ok(name, actual === expected, "got " + JSON.stringify(actual) + " want " + JSON.stringify(expected))
}

console.log("parseStatus: connected")
{
  const raw = readFileSync("/tmp/ws-status-conn.txt", "utf8")
  const s = Model.parseStatus(raw)
  ok("signedIn", s.signedIn === true)
  ok("connected", s.connected === true)
  eq("where", s.where, "Toronto - Comfort Zone")
  eq("protocol", s.protocol, "WireGuard:443")
  eq("ip", s.ip, "149.88.98.88 (VPN)")
  eq("firewall", s.firewall, "On")
  eq("dataUsage", s.dataUsage, "96.61 MB / Unlimited")
  eq("internet", s.internet, "Available")
  ok("statusComplete", Model.statusComplete(raw) === true)
  const place = Model.splitPlace(s.where)
  eq("city", place.city, "Toronto")
  eq("nickname", place.nickname, "Comfort Zone")
}

console.log("parseStatus: disconnected + logged out")
{
  const raw = "Login:     Logged out\nIP:        ---.---.---.--- (public)\nFirewall:  Off\nInternet:  Available\n"
  const s = Model.parseStatus(raw)
  ok("signedOut", s.signedIn === false)
  ok("notConnected", s.connected === false && s.connecting === false)
  eq("firewall", s.firewall, "Off")
  ok("statusComplete", Model.statusComplete(raw) === true)
}

console.log("parseStatus: connecting, checking, interference, error")
{
  const s = Model.parseStatus("Login: Logged in\nConnection: Connecting to Tokyo - Shibuya\n")
  ok("connecting", s.connecting === true && s.connected === false)
  const c = Model.parseStatus("Login: Logged in\nConnection: Connected to Toronto - Comfort Zone (checking the connection)\n")
  ok("checking", c.connected === true && c.checking === true && c.where === "Toronto - Comfort Zone")
  const i = Model.parseStatus("Login: Logged in\nConnection: Connected to Toronto - Comfort Zone (network interference detected)\n")
  ok("interference", i.connected === true && i.interference === true)
  const e = Model.parseStatus("Login: Error: auth failed\nConnection: Disconnected\n")
  ok("loginError", e.signedIn === false && e.loginError === "auth failed")
}

console.log("parseLocations / groupLocations")
{
  const raw = readFileSync("/tmp/ws-locations.txt", "utf8")
  const list = Model.parseLocations(raw)
  ok("parses 202", list.length === 202, "got " + list.length)
  const best = list[0]
  eq("best region", best.region, "Best Location")
  eq("best city", best.city, "Comfort Zone")
  ok("best 10gbps", best.tenGbps === true)
  const regions = Model.groupLocations(list)
  ok("76 regions", regions.length === 76, "got " + regions.length)
  const use = regions.find(r => r.name === "US East")
  ok("US East exists", !!use)
  const ny = use.cities.find(c => c.name === "New York")
  ok("NY has nicknames", ny && ny.nicknames.indexOf("Big Apple") !== -1)
  ok("NY 10gbps", ny && ny.tenGbps === true)
  const filtered = Model.filterRegions(regions, "japan")
  ok("filter japan", filtered.length === 1 && filtered[0].name === "Japan")
  const filteredCity = Model.filterRegions(regions, "tokyo")
  ok("filter city", filteredCity.length === 1 && filteredCity[0].cities.length === 1)
}

console.log("parseStatics")
{
  const raw = readFileSync("/tmp/ws-static.txt", "utf8")
  const list = Model.parseStatics(raw)
  ok("one static", list.length === 1, "got " + list.length)
  eq("city", list[0] && list[0].city, "Toronto")
  eq("ip", list[0] && list[0].ip, "206.223.183.218")
}

console.log("connectedLine / parseConnected")
{
  const raw = readFileSync("/tmp/ws-connect.txt", "utf8")
  const line = Model.connectedLine(raw)
  ok("line found", /^Connected to Toronto - Comfort Zone$/.test(line), "got " + JSON.stringify(line))
  const p = Model.parseConnected(raw)
  eq("city", p && p.city, "Toronto")
  eq("nickname", p && p.nickname, "Comfort Zone")
}

console.log("username quoting")
{
  ok("valid", Model.validUsername("alice"))
  ok("valid email", Model.validUsername("alice@example.com"))
  ok("rejects quote", Model.validUsername("alice'; rm -rf") === false)
  ok("rejects space", Model.validUsername("al ice") === false)
  eq("shellQuote", Model.shellQuote("it's"), "'it'\\''s'")
  eq("escapeMarkup", Model.escapeMarkup("<b>&"), "&lt;b&gt;&amp;")
}

console.log("conf file INI reading")
{
  const conf = "[General]\nLanguage=en\n\n[Connection]\nAutoconnect=false\nFirewallMode=Auto\nSplitTunnelingEnabled=false\nSplitTunnelingApps=\n\n[Advanced]\nDNSManager=Auto\n"
  const section = Model.parseConfSection(conf, "Connection")
  eq("autoconnect", section && section.Autoconnect, "false")
  eq("empty value", section && section.SplitTunnelingApps, "")
  eq("missing section", Model.parseConfSection(conf, "Nope"), null)
}

console.log("QSettings-compatible string lists")
{
  // Qt's writer: a comma (or `;` or `=`) quotes the whole element, a
  // quote and a backslash are escaped, control characters become C-style
  // escapes, and elements are joined with ", ".
  eq("plain path", Model.encodeIniString("/usr/bin/firefox"), "/usr/bin/firefox")
  eq("comma quotes", Model.encodeIniString("/opt/My,App/bin/app"), '"/opt/My,App/bin/app"')
  eq("quote escapes", Model.encodeIniString('/opt/we"ird/app'), '/opt/we\\"ird/app')
  eq("backslash escapes", Model.encodeIniString("/opt/back\\slash/app"), "/opt/back\\\\slash/app")
  eq("semicolon quotes", Model.encodeIniString("/opt/semi;colon/app"), '"/opt/semi;colon/app"')
  eq("equals quotes", Model.encodeIniString("/opt/eq=uals/app"), '"/opt/eq=uals/app"')
  eq("padded quotes", Model.encodeIniString(" /opt/spacey/app "), '" /opt/spacey/app "')
  eq("tab escapes", Model.encodeIniString("/opt/tab\there/app"), "/opt/tab\\there/app")
  eq("list joins with comma-space", Model.encodeIniStringList(["/usr/bin/a", "/opt/b,c/d"]),
     '/usr/bin/a, "/opt/b,c/d"')
  eq("empty list stays empty value", Model.encodeIniStringList([]), "")
  // A path with a comma is one entry after a round-trip, not two.
  eq("comma path is one entry", Model.parseIniStringList('"x,y", z').length, 2)
  eq("comma entry survives", Model.parseIniStringList('"x,y", z')[0], "x,y")
  // Qt's reader: unquoted commas split, escapes resolve, spaces around
  // elements are dropped unless the element was quoted.
  eq("parse plain list", Model.parseIniStringList("/usr/bin/a, /usr/bin/b").join("|"), "/usr/bin/a|/usr/bin/b")
  eq("parse escapes", Model.parseIniStringList("a\\tb, c\\\\d, e\\\"f").join("|"), "a\tb|c\\d|e\"f")
  eq("parse unknown escape drops both", Model.parseIniStringList("a\\qb")[0], "ab")
  eq("parse single is one element", Model.parseIniStringList("/usr/bin/only").length, 1)
  eq("parse unquoted spaces dropped", Model.parseIniStringList("  a  ,  b  ").join("|"), "a|b")
  eq("parse quoted spaces kept", Model.parseIniStringList('" a " , b').join("|"), " a |b")
  eq("parse empty value", Model.parseIniStringList("")[0], "")
  eq("parse empty element kept", Model.parseIniStringList("a, , b").join("|"), "a||b")
  // The full round-trip, nastiest cases included: control characters and
  // a 0x1F followed by hex digits (Qt re-escapes the digits so the pair
  // reads back intact).
  const paths = [
    "/usr/bin/firefox",
    "/opt/My,App/bin/app",
    '/opt/we"ird/app',
    "/opt/back\\slash/app",
    "/opt/semi;colon/app",
    "/opt/eq=uals/app",
    " /opt/spacey/app ",
    "/opt/tab\there/app",
    "/opt/new\nline/app",
    "/opt/hex\x1fe5c/app",
    "",
    "/usr/bin/zsh"
  ]
  const round = Model.parseIniStringList(Model.encodeIniStringList(paths))
  ok("round-trip", paths.every((p, i) => round[i] === p),
     "got " + JSON.stringify(round))
}

console.log("confedit.py: one setting, one write, confirmed")
{
  const helper = join(dir, "..", "confedit.py")
  const work = mkdtempSync(join(tmpdir(), "ws-conf-"))
  const path = join(work, "windscribe_cli.conf")
  const original =
    "[General]\nLanguage=en\n\n[Connection]\nAutoconnect=false\nFirewallMode=Auto\nSplitTunnelingEnabled=false\nSplitTunnelingApps=\n\n[Advanced]\nDNSManager=Auto\n"
  writeFileSync(path, original, { mode: 0o600 })
  const run = (args) => spawnSync("python3", [helper, path, ...args], { encoding: "utf8" })

  const r1 = run(["Connection", "FirewallMode", "Always On"])
  ok("writes and confirms", r1.status === 0, "exit " + r1.status + " stderr " + r1.stderr)
  eq("prints the landed value", r1.stdout.trim(), "Always On")
  const after = readFileSync(path, "utf8")
  eq("one line changed, byte-exact", after,
     original.replace("FirewallMode=Auto", "FirewallMode=Always On"))
  ok("no extra trailing newline", /\n$/.test(after) && !/\n\n$/.test(after))
  ok("file mode kept", (statSync(path).mode & 0o777) === 0o600)

  const r2 = run(["Connection", "Nope", "x"])
  ok("missing key refused", r2.status === 3, "exit " + r2.status)
  ok("refusal leaves the file alone", readFileSync(path, "utf8") === after)

  const r3 = run(["NoSuchSection", "FirewallMode", "Auto"])
  ok("missing section refused", r3.status === 3, "exit " + r3.status)

  const absent = join(work, "absent.conf")
  const r4 = spawnSync("python3", [helper, absent, "Connection", "FirewallMode", "Auto"], { encoding: "utf8" })
  ok("missing file refused", r4.status === 3, "exit " + r4.status)
  ok("missing file never created", !existsSync(absent))

  const r5 = run(["Connection", "SplitTunnelingApps", "a\nb"])
  ok("multi-line value refused", r5.status === 4, "exit " + r5.status)
  ok("refused value leaves the file alone", readFileSync(path, "utf8") === after)

  // The coordination property this helper exists for: a newer external
  // change (the engine's own save, simulated here) is read fresh and kept,
  // where a cached-copy writer would have reverted it.
  const withExternal = after.replace("Autoconnect=false", "Autoconnect=true")
  writeFileSync(path, withExternal)
  const r6 = run(["Connection", "SplitTunnelingEnabled", "true"])
  ok("save against a fresh read", r6.status === 0, "exit " + r6.status + " stderr " + r6.stderr)
  const kept = readFileSync(path, "utf8")
  ok("newer external change kept", kept.indexOf("Autoconnect=true") !== -1)
  ok("own key written", kept.indexOf("SplitTunnelingEnabled=true") !== -1)
  ok("nothing else touched", kept === withExternal.replace("SplitTunnelingEnabled=false", "SplitTunnelingEnabled=true"))
}

console.log("")
console.log(pass + " passed, " + fail + " failed")
process.exit(fail === 0 ? 0 : 1)
