// EXECUTE the dashboard's own script and report what it rendered.
//
// The one screen a person reads had every assertion pointed at its DATA —
// `from_store()` returns the right numbers — and none at whether the page turns
// those numbers into anything. The health panel was filled four times in one
// night without the page ever being opened, and a JavaScript error would have
// left it blank with every Python test still green.
//
// A browser was not reachable (the Chrome extension is not connected on this
// machine), so this is the honest substitute: node, a minimal DOM that records
// what the script writes, and the script's real output on stdout as JSON. It
// catches a syntax error, an undefined reference and an empty panel — which is
// what "checked in a browser" was protecting against — and it does not pretend
// to check layout, fonts or colour.
//
//   node tests/render_dashboard.mjs docs/projects-dashboard.html
import { readFileSync } from "node:fs";

const file = process.argv[2];
const html = readFileSync(file, "utf8");

const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (scripts.length === 0) {
  console.log(JSON.stringify({ error: "the page carries no script" }));
  process.exit(1);
}

// The elements the page writes to, discovered from the page rather than listed
// here: a hardcoded list would drift the moment a panel is added.
const ids = new Set([...html.matchAll(/getElementById\("([^"]+)"\)/g)].map(m => m[1]));
for (const m of html.matchAll(/id="([^"]+)"/g)) ids.add(m[1]);

const rootAttributes = Object.fromEntries(
  [...((html.match(/<html\b([^>]*)>/) || [])[1] || "").matchAll(/([a-z-]+)="([^"]*)"/g)]
    .map(m => [m[1], m[2]]));
const written = {};

// A bounded excerpt that names its own limit, so a reader of this report cannot
// mistake the excerpt for the whole.
function clip(text, limit) {
  const s = String(text || "");
  return s.length <= limit ? s
    : s.slice(0, limit) + `… [truncated by the harness: ${s.length - limit} more chars of ${s.length}]`;
}
const listeners = [];

function makeEl(id) {
  const el = {
    id,
    _html: "",
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); written[id] = this._html; },
    get textContent() { return this._html.replace(/<[^>]*>/g, " "); },
    set textContent(v) { this.innerHTML = String(v); },
    style: {setProperty() {}, removeProperty() {}, getPropertyValue() { return ""; }},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    dataset: {},
    addEventListener(type, fn) { listeners.push([id, type, fn]); },
    removeEventListener() {},
    appendChild() {},
    setAttribute() {},
    getAttribute() { return null; },
    focus() {},
    offsetHeight: 0,
    offsetWidth: 0,
    getBoundingClientRect() { return {top: 0, left: 0, width: 0, height: 0}; },
    scrollIntoView() {},
    value: "",
    checked: false,
    children: [],
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
  return el;
}

const elements = new Map();
for (const id of ids) elements.set(id, makeEl(id));

const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl(id));
    return elements.get(id);
  },
  // An ELEMENT, not null. The page reads `.offsetHeight` off a selector result,
  // which a browser would have found; returning null made the run die with
  // `Cannot read properties of null` after the findings panel — the third
  // harness gap mistaken for a page defect in as many runs.
  querySelector() { return makeEl("<selector>"); },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl(`<${tag}>`); },
  addEventListener(type, fn) { listeners.push(["document", type, fn]); },
  body: makeEl("body"),
  // The root element answers with the page's own attributes, so the script
  // reads the language the page was built in (`data-build-locale`) as a
  // browser would; a stub answering null made every page English.
  documentElement: Object.assign(makeEl("html"), {
    getAttribute(name) { return rootAttributes[name] ?? null; },
  }),
};

const errors = [];
const globals = {
  document,
  window: {
    addEventListener(type, fn) { listeners.push(["window", type, fn]); },
    location: { hash: "", href: `file://${file}`, search: "" },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    devicePixelRatio: 1,
  },
  location: { hash: "", href: `file://${file}`, search: "" },
  console: { log() {}, warn() {}, error(...a) { errors.push(a.join(" ")); } },
  // BARE globals the page uses without a receiver. In a browser
  // `addEventListener(...)` resolves to `window.addEventListener`; inside the
  // function scope this harness builds, the name simply does not exist — so the
  // first run reported `ReferenceError: addEventListener is not defined` and I
  // nearly recorded it as a page defect. It was the stub's gap, and the
  // distinction matters: a harness that fails differently from a browser tests
  // the harness.
  addEventListener(type, fn) { listeners.push(["global", type, fn]); },
  removeEventListener() {},
  // The only real browser CONSTRUCTOR the page needs, measured rather than
  // guessed: `new ResizeObserver(...)`. `prompt` and `confirm` appear eleven and
  // five times in the file and are WORDS inside embedded finding text — "confirm
  // the card on file is current" — not calls. A raw regex over the whole page
  // counted them as code once already, which is the same mistake as reading the
  // twenty-two occurrences of "fetch" as network calls on a self-contained page.
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  requestAnimationFrame: (fn) => fn(),
  setTimeout: (fn) => fn(),
  navigator: { userAgent: "node" },
};
globals.window.document = document;
globals.globalThis = globals;

let threw = null;
try {
  const body = scripts.join("\n;\n");
  const names = Object.keys(globals);
  // eslint-disable-next-line no-new-func
  const run = new Function(...names, `"use strict";\n${body}`);
  run(...names.map(n => globals[n]));
} catch (e) {
  threw = `${e.name}: ${e.message}`;
}

// HOW MANY TIMES a pattern appears in what the script wrote, per panel. The
// biggest panel is 268 KB and no excerpt of it can honestly answer "is this
// marker rendered" — a clip either finds the pattern by luck or misses it by
// luck. A count is bounded, exact, and cannot be mistaken for the whole.
//
//   node tests/render_dashboard.mjs page.html --count 'class="delta'
const needle = (() => {
  const i = process.argv.indexOf("--count");
  return i > 0 ? process.argv[i + 1] : null;
})();
const counts = {};
if (needle !== null) {
  for (const [id, v] of Object.entries(written)) {
    let n = 0, from = 0;
    for (;;) {
      const at = v.indexOf(needle, from);
      if (at < 0) break;
      n++; from = at + needle.length;
    }
    if (n) counts[id] = n;
  }
}

console.log(JSON.stringify({
  threw,
  consoleErrors: errors,
  ...(needle === null ? {} : {counted: needle, counts}),
  written: Object.fromEntries(Object.entries(written).map(([k, v]) => [k, v.length])),
  health: written.health || "",
  // BOUNDED, AND IT SAYS SO. `slice(0, 400)` alone returned 400 characters of a
  // 20 KB render with no marker, and it produced a false measurement inside one
  // hour: counting rendered rows in that report said the page shows ONE finding
  // of sixty-three, where it carries forty. A silent cap in a test instrument is
  // a wrong conclusion waiting to be drawn from it.
  findings: clip(written.findings, 400),
  tiles: clip(written.tiles, 200),
  listeners: listeners.length,
}, null, 1));
