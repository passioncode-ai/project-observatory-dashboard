// Where KEYBOARD FOCUS lands after a control re-renders the list it sits in.
//
// WHY A THIRD HARNESS. `dashboard/smoke.js` asks whether the script throws,
// `tests/render_dashboard.mjs` what it writes and `tests/env_tab_check.js`
// whether the ENV tab filters; none of them can say where focus goes, because
// their elements have no `focus()` that records anything. Two defects lived in
// that gap: a sortable column header and the "reset" button of an empty result
// both re-render the table they belong to, the control that held focus is
// replaced, and focus fell to <body> — a keyboard reader pressed Enter and was
// thrown back to the top of the page.
//
// WHAT IT CAN AND CANNOT SAY. This is a stub DOM, not a browser: every element
// a selector names is a stable stub keyed by that selector, and `focus()`
// records the stub as `document.activeElement`. So it proves that the handler
// asks for the RE-RENDERED control by its key and focuses it, which is the
// behaviour; it does not prove the selector matches the real markup. That half
// is asserted against the page source by the Python test that drives this.
//
//     node tests/focus_check.js pages/projects.html
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const file = process.argv[2];
const html = fs.readFileSync(file, "utf8");
// Inline scripts in document order, and `src` scripts read from beside the
// page: the split pages carry their shared code in `app.js`.
const parts = [];
for (const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
  const src = (m[1].match(/\bsrc="([^"]+)"/) || [])[1];
  parts.push(src ? fs.readFileSync(path.join(path.dirname(file), src), "utf8") : m[2]);
}

const listeners = [];
const doc = { activeElement: null };
const nodes = new Map();
function node(key) {
  if (nodes.has(key)) return nodes.get(key);
  const n = {
    key, innerHTML: "", textContent: "", value: "", hidden: false, open: false,
    dataset: {}, children: [], isConnected: true, offsetHeight: 0, offsetParent: {},
    style: { setProperty() {}, removeProperty() {}, getPropertyValue() { return ""; } },
    classList: { add() {}, remove() {}, toggle: () => false, contains: () => false },
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(type, fn) { listeners.push([key, type, fn]); },
    removeEventListener() {},
    appendChild() {}, removeChild() {}, remove() {}, before() {}, after() {}, select() {},
    scrollIntoView() {},
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
    focus() { doc.activeElement = this; },
    querySelector(sel) { return node(key + " " + sel); },
    querySelectorAll() { return []; },
    closest() { return null; },
  };
  nodes.set(key, n);
  return n;
}
const document = {
  get activeElement() { return doc.activeElement; },
  documentElement: Object.assign(node("html"), { getAttribute: () => null }),
  body: node("body"),
  getElementById: id => node("#" + id),
  // The keyserver's token is ABSENT: a page opened from a file. Everything else
  // a selector names exists.
  querySelector: sel => (sel.includes("observatory-token") ? null : node("doc " + sel)),
  querySelectorAll: () => [],
  createElement: tag => node("<" + tag + ">"),
  addEventListener(type, fn) { listeners.push(["document", type, fn]); },
  execCommand: () => true,
};
const location = { hash: "", search: "", href: "file://" + file, protocol: "file:",
                   assign() {}, replace() {} };
const storage = { getItem: () => null, setItem() {}, removeItem() {} };
const context = {
  document, location, URLSearchParams, Intl,
  addEventListener(type, fn) { listeners.push(["window", type, fn]); },
  removeEventListener() {},
  setTimeout: () => 0, clearTimeout() {},
  requestAnimationFrame: fn => fn(),
  navigator: {},
  localStorage: storage, sessionStorage: storage,
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  console: { log() {}, warn() {}, error() {} },
};
context.window = Object.assign(context, {
  matchMedia: () => ({ matches: false, addEventListener() {} }),
});
context.globalThis = context;

const result = { threw: null };
try {
  vm.createContext(context);
  vm.runInContext(parts.join("\n;\n"), context, { filename: path.basename(file), timeout: 20000 });
} catch (e) {
  result.threw = `${e.name}: ${e.message}`;
}

// A click whose target sits inside the element `selector` names, delivered to
// every click listener the page registered (each one filters by `closest`).
function click(selector, inside) {
  const target = { closest: s => (s === selector ? inside : null) };
  doc.activeElement = null;
  const errors = [];
  for (const [, type, fn] of listeners) {
    if (type !== "click") continue;
    try { fn({ target, preventDefault() {}, stopPropagation() {} }); }
    catch (e) { errors.push(`${e.name}: ${e.message}`); }
  }
  return { focused: doc.activeElement ? doc.activeElement.key : null, errors };
}

// `--eval EXPR`: the value of an expression evaluated in the loaded page,
// for a helper whose output is text (what a toast will say).
const evalAt = process.argv.indexOf("--eval");
if (!result.threw && evalAt > 0) {
  try { result.value = vm.runInContext(process.argv[evalAt + 1], context); }
  catch (e) { result.evalError = `${e.name}: ${e.message}`; }
}
if (!result.threw) {
  result.sort = click("th[data-sort]", Object.assign(node("old th"), { dataset: { sort: "name" } }));
  result.clear = click("[data-clear]", node("old reset"));
  result.fclear = click("[data-fclear]", node("old freset"));
}
console.log(JSON.stringify(result));
