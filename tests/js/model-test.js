// Model.js protocol helpers, run under node when available (optional).
const fs = require("fs")
const path = require("path")
const assert = require("assert")
const src = fs.readFileSync(path.join(__dirname, "..", "..", "Model.js"), "utf8").replace(/^\.pragma library/m, "")
const M = {}
new Function("exports", src + "\nfor (const k of ['splitLines','parseEvent','sanitizeStatus','sanitizeControls','retryable','retryDelay','localPath','MAX_LINE','worn','headTransition']) exports[k] = eval(k)")(M)

// bounded line splitting
let r = M.splitLines("", '{"type":"a"}\n{"ty')
assert.deepStrictEqual(r, { lines: ['{"type":"a"}'], rest: '{"ty', overflow: false })
r = M.splitLines(r.rest, 'pe":"b"}\n')
assert.deepStrictEqual(r.lines, ['{"type":"b"}'])
assert.strictEqual(M.splitLines("", "x".repeat(M.MAX_LINE)).overflow, true)
assert.strictEqual(M.splitLines("", "x".repeat(M.MAX_LINE) + "\n").overflow, true)

// events
assert.strictEqual(M.parseEvent("not json"), null)
assert.strictEqual(M.parseEvent("[1]"), null)
assert.strictEqual(M.parseEvent('{"type":"x","n":"é"}'), null)   // non-ASCII rejected
assert.deepStrictEqual(M.parseEvent('{"type":"ready"}'), { type: "ready" })

// status sanitizing
const st = M.sanitizeStatus({ connected: "1", addr: "AA:BB:CC:DD:EE:FF", name: "<img src=x>", anc: "loud",
  left: 90, right: 101, case: -1, left_state: "charging", right_state: "exploding", left_in_case: 1,
  case_last_age: -5, error: "x".repeat(300), evil: "y", adaptive_supported: "1" })
assert.deepStrictEqual(st, { connected: "1", addr: "AA:BB:CC:DD:EE:FF", name: "<img src=x>", anc: "unknown",
  left: 90, case: -1, left_state: "charging", left_in_case: 1, adaptive_supported: "1" })
assert.deepStrictEqual(M.sanitizeStatus(null), {})

// controls sanitizing
const c = M.sanitizeControls({ ctl_mono: "true", ctl_ohd: true, ctl_gesture_left: "anc", ctl_gesture_right: "play",
  ctl_anc_gesture_loop: "active,aware", ctl_balance: 20.5, ctl_eq: "0.00,1.50,-2.00,0.50,3.00", ctl_x: "1" })
assert.deepStrictEqual(c, { ctl_mono: "true", ctl_gesture_left: "anc", ctl_anc_gesture_loop: "active,aware",
  ctl_eq: "0.00,1.50,-2.00,0.50,3.00" })
assert.strictEqual(M.sanitizeControls({ ctl_anc_gesture_loop: "active;rm" }).ctl_anc_gesture_loop, undefined)

// retry policy
assert.ok(M.retryable("link_lost") && M.retryable("crashed") && !M.retryable("absent") && !M.retryable("disconnected"))
assert.deepStrictEqual([1, 2, 3, 9].map(M.retryDelay), [1000, 3000, 10000, 60000])
assert.strictEqual(M.localPath("file:///a%20b/x.py"), "/a b/x.py")
// ear detection
const both = { left: true, right: true }
assert.strictEqual(M.worn(null, {}), null)
assert.strictEqual(M.worn({ left: true }, {}), null)
assert.deepStrictEqual(M.worn(both, { left_in_case: 1 }), { left: false, right: true })   // docked = off-head
assert.strictEqual(M.headTransition(null, both), "")                                   // first report: no action
assert.strictEqual(M.headTransition(both, { left: false, right: true }), "off")
assert.strictEqual(M.headTransition({ left: false, right: true }, { left: false, right: false }), "off")
assert.strictEqual(M.headTransition({ left: false, right: true }, both), "on")
assert.strictEqual(M.headTransition({ left: false, right: false }, { left: true, right: false }), "")
assert.strictEqual(M.headTransition(both, both), "")
console.log("model test passed")
