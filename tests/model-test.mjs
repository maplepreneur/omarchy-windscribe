// Parser tests for Model.js, run with: node tests/model-test.mjs
// Every fixture is real output captured from windscribe-cli 2.25.8.
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { dirname, join } from "node:path"

const dir = dirname(fileURLToPath(import.meta.url))
globalThis.dir = dir
const source = readFileSync(join(dir, "..", "Model.js"), "utf8")
// Model.js is a plain script; evaluate it in this scope.
const Model = new Function(source + "\n; return { stripAnsi, parseKeyValues, parseStatus, statusComplete, splitPlace, escapeMarkup, parseLocations, groupLocations, parseStatics, connectedLine, parseConnected, filterRegions, elide, validUsername, shellQuote, parseConfSection, setConfValue };")()

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

console.log("conf file INI editing")
{
  const conf = "[General]\nLanguage=en\n\n[Connection]\nAutoconnect=false\nFirewallMode=Auto\nSplitTunnelingEnabled=false\nSplitTunnelingApps=\n\n[Advanced]\nDNSManager=Auto\n"
  const section = Model.parseConfSection(conf, "Connection")
  eq("autoconnect", section && section.Autoconnect, "false")
  eq("empty value", section && section.SplitTunnelingApps, "")
  eq("missing section", Model.parseConfSection(conf, "Nope"), null)
  const edited = Model.setConfValue(conf, "Connection", "SplitTunnelingEnabled", "true")
  const reparsed = Model.parseConfSection(edited, "Connection")
  eq("value set", reparsed.SplitTunnelingEnabled, "true")
  eq("others intact", reparsed.Autoconnect, "false")
  eq("general intact", Model.parseConfSection(edited, "General").Language, "en")
  eq("advanced intact", Model.parseConfSection(edited, "Advanced").DNSManager, "Auto")
  ok("unknown key refused", Model.setConfValue(conf, "Connection", "Nope", "x") === "")
  // trailing newline preserved
  ok("newline kept", /\n$/.test(Model.setConfValue("x\n[Connection]\nAutoconnect=false\n", "Connection", "Autoconnect", "true")))
  ok("no-newline kept", !/\n$/.test(Model.setConfValue("x\n[Connection]\nAutoconnect=false", "Connection", "Autoconnect", "true")))
}

console.log("")
console.log(pass + " passed, " + fail + " failed")
process.exit(fail === 0 ? 0 : 1)
