// Drive the ENV tab of the built page against a stub DOM that has IDENTITY.
//
// WHY A SECOND HARNESS. `dashboard/smoke.js` answers one question — does the
// script throw at load — and answers it well. It cannot answer this one,
// because its `getElementById` returns a FRESH node on every call: nothing the
// script writes can be read back, so a tab that renders an empty table and a
// tab that renders a hundred rows are indistinguishable to it. Giving smoke
// identity would change what every other assertion in it means; a second file
// costs less than that.
//
// WHAT IT DRIVES, and the browser check it replaces is named rather than
// implied: the Chrome MCP on this machine was holding a profile lock from
// another session on 2026-09-12, so the tab was verified by EXECUTING the
// page's own script rather than by clicking it. This runs on every gate, which
// the click would not have.
//
//     node tests/env_tab_check.js docs/projects-dashboard.html
"use strict";
const fs = require("fs");
const vm = require("vm");

const file = process.argv[2] || "docs/projects-dashboard.html";
const html = fs.readFileSync(file, "utf8");
const start = html.lastIndexOf("<script>");
const end = html.lastIndexOf("</script>");
if (start < 0 || end < 0) {
  console.error("FAIL  the page carries no <script>");
  process.exit(1);
}
const source = html.slice(start + 8, end);

const failures = [];
function check(name, ok, detail) {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}` + (!ok && detail ? ` — ${detail}` : ""));
  if (!ok) failures.push(name);
}

// ── a stub DOM that remembers ──────────────────────────────────────────────
function makeDom(opts) {
  const nodes = new Map();
  const chips = [];
  function node(id) {
    return {
      id, innerHTML: "", textContent: "", value: "", hidden: false,
      dataset: {}, children: [], isConnected: true,
      style: { setProperty() {} },
      classList: { add() {}, remove() {}, toggle: () => false, contains: () => false },
      offsetHeight: 56, offsetParent: {},
      attrs: {},
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
      addEventListener() {}, removeEventListener() {},
      appendChild() {}, removeChild() {}, select() {},
      querySelector: () => null, querySelectorAll: () => [],
      closest: () => null,
    };
  }
  function byId(id) {
    if (!nodes.has(id)) nodes.set(id, node(id));
    return nodes.get(id);
  }
  // The six ENV chips, as the page's own selector would find them.
  for (const f of ["e-shared", "e-reuse", "e-git", "e-open", "e-all", "e-tpl"]) {
    const c = node("chip-" + f);
    c.dataset.f = f;
    c.setAttribute("aria-pressed", "false");
    chips.push(c);
  }
  const document = {
    documentElement: { setAttribute() {}, getAttribute: () => "en", style: { setProperty() {} } },
    body: { appendChild() {}, removeChild() {} },
    getElementById: byId,
    createElement: () => node("created"),
    execCommand: () => true,
    querySelector(sel) {
      // THE ONE SELECTOR THAT DECIDES `LIVE`. Returning an object with a
      // `content` is how this harness pretends the keyserver injected its token.
      if (sel.includes("observatory-token")) {
        return opts.token ? { content: opts.token } : null;
      }
      return byId("sel");
    },
    querySelectorAll: sel => (sel.includes("chip-btn") ? chips : []),
    addEventListener() {},
  };
  const location = {
    hash: opts.hash || "", search: opts.search || "",
    href: opts.href || "file:///dashboard",
    protocol: opts.protocol || "file:", assign() {}, replace() {},
  };
  const context = {
    document, location,
    addEventListener() {}, removeEventListener() {},
    setTimeout: () => 0, clearTimeout() {},
    navigator: {},
    window: { matchMedia: () => ({ matches: false, addEventListener() {} }) },
    ResizeObserver: class { observe() {} },
    // The page reads the address for a pressed filter chip; both of
    // these are things every browser provides and this stub did not.
    URLSearchParams, Intl,
    // The reader's language choice, as the EN/RU switch stores it.
    localStorage: { getItem: k => (k === "observatory.locale" ? opts.locale || null : null),
                    setItem() {}, removeItem() {} },
    console: { log() {}, warn() {}, error() {} },
  };
  context.window.document = document;
  context.window.location = location;
  context.window.addEventListener = context.addEventListener;
  context.matchMedia = context.window.matchMedia;
  return { context, byId, chips };
}

function run(opts) {
  const dom = makeDom(opts);
  vm.createContext(dom.context);
  vm.runInContext(source, dom.context, { filename: "env-tab", timeout: 20000 });
  return dom;
}

// ── the static page: no token, opened from a file ──────────────────────────
let dom;
try {
  dom = run({ hash: "#env" });
} catch (e) {
  check("the ENV tab renders without throwing", false, String(e && e.message || e));
  console.log("\n\x1b[31m1 failed\x1b[0m");
  process.exit(1);
}
const out = dom.byId("out").innerHTML;
check("the ENV tab renders without throwing", true);
check("it writes a table rather than an empty state", out.includes("<table>"), out.slice(0, 120));
// The header cell is a sortable button: the word is
// still there, wrapped in `th[data-sort="name"] > button.sort`.
check("the table is the ENV one",
      out.includes("<th>Variable</th>") || /<th aria-sort="[a-z]+" data-sort="name"><button class="sort"[^>]*>Variable<\/button><\/th>/.test(out),
      out.slice(0, 200));
const rows = (out.match(/<tr id="e-/g) || []).length;
check("it renders rows", rows > 1, `${rows} rows`);
check("the tab count is filled", dom.byId("n-env").textContent !== "", "empty");

// THE BOUNDARY THIS FEATURE RESTS ON. A page opened from a file cannot read a
// value, so it must not offer to: the control it renders is the command, and
// the reveal button belongs only to the served build.
check("a file:// page offers a command, never a reveal",
      out.includes("copy the command") && !out.includes(">show<"),
      out.includes(">show<") ? "it rendered a reveal button" : "no command button");
check("no value-shaped payload reached the page",
      !html.includes('"fingerprint"'),
      "the page carries a fingerprint, which belongs only to the gitignored scan");

// ── widening: templates are hidden until asked for ─────────────────────────
const before = rows;
const tpl = dom.chips.find(c => c.dataset.f === "e-tpl");
tpl.onclick();
const after = (dom.byId("out").innerHTML.match(/<tr id="e-/g) || []).length;
check("the `+ templates` chip widens the selection", after > before, `${before} -> ${after}`);
tpl.onclick();
const back = (dom.byId("out").innerHTML.match(/<tr id="e-/g) || []).length;
check("and releasing it narrows again", back === before, `${before} -> ${after} -> ${back}`);

// ── narrowing: shared-only is a strict subset ──────────────────────────────
const shared = dom.chips.find(c => c.dataset.f === "e-shared");
shared.onclick();
const sharedRows = (dom.byId("out").innerHTML.match(/<tr id="e-/g) || []).length;
check("`shared with another project` narrows", sharedRows < before && sharedRows > 0,
      `${before} -> ${sharedRows}`);
check("matching groups are open while a filter is active", !dom.byId("out").innerHTML.includes('class="grp folded"'));
check("one variable uses the singular form", /\b1 variable(?!s)/.test(dom.byId("out").innerHTML) && !dom.byId("out").innerHTML.includes('1 variables'));
{
  // Russian has three forms; the page chooses by Intl.PluralRules, not by hand.
  const ru = run({ hash: "#env", locale: "ru" });
  ru.chips.find(c => c.dataset.f === "e-shared").onclick();
  const o = ru.byId("out").innerHTML;
  check("in Russian one variable is «1 переменная»", o.includes("1 переменная") && !o.includes("1 переменных"), o.slice(0, 160));
  check("and the Russian table heads in Russian", o.includes("Переменная</button>"), o.slice(0, 160));
}
shared.onclick();
check("clearing filters keeps the record list visible", !dom.byId("out").innerHTML.includes('class="grp folded"'));
const commandHttp = run({hash:"#env", protocol:"http:", href:"http://127.0.0.1:7717/"});
const commandText = commandHttp.byId("out").innerHTML;
check("HTTP without an action token describes command mode", commandText.includes('command mode') && !commandText.includes('opened from a file'));
check("command mode does not claim execution", commandText.includes('run it in a terminal') && !commandText.includes('>show<'));

// ── two apps compared with one folder: both verdicts are shown ─────────────
{
  const inject = 'const REMOTE_BY_FOLDER = new Map();';
  const twoApps = inject.replace('new Map();', 'new Map(); (() => { const f = ENVF.find(x => x.kind === "env" && x.variables.length); if (!f) return; ' +
    'const name = f.variables[0].name; const folder = String(f.project); ' +
    'D.remote = { apps: [ { app: "fixture-prod", compared_with: [folder], vars: [{ name, verdict: "same_as_local" }] }, ' +
    '{ app: "fixture-staging", compared_with: [folder], vars: [{ name, verdict: "differs" }] } ] }; })();');
  const patched = source.includes(inject) ? source.replace(inject, twoApps) : null;
  if (!patched) {
    check("two apps of one folder are both shown", false, "the page no longer declares REMOTE_BY_FOLDER");
  } else {
    const dom2 = makeDom({ hash: "#env" });
    vm.createContext(dom2.context);
    vm.runInContext(patched, dom2.context, { filename: "env-tab-two-apps", timeout: 20000 });
    const o2 = dom2.byId("out").innerHTML;
    check("two apps of one folder are both shown", o2.includes("fixture-prod") && o2.includes("fixture-staging"),
          o2.includes("fixture-prod") ? "only the first app" : "neither app");
    check("and the dangerous verdict comes first",
          o2.indexOf("fixture-prod") > -1 && o2.indexOf("fixture-prod") < o2.indexOf("fixture-staging"));
  }
}

// ── the served page: a token is present and the protocol is http ───────────
let live;
try {
  live = run({ hash: "#env", token: "stub", protocol: "http:", href: "http://127.0.0.1:7717/" });
} catch (e) {
  check("the served build renders", false, String(e && e.message || e));
}
if (live) {
  const lout = live.byId("out").innerHTML;
  check("the served build renders", true);
  check("and it offers the reveal instead of the command",
        lout.includes(">show<") && !lout.includes("copy the command"),
        lout.includes("copy the command") ? "it still renders the command" : "no reveal button");
  check("the reveal names its variable, so a click cannot mean another row",
        /data-env="[^"]+ [A-Z_]/.test(lout), "no data-env carrying a path and a name");
}

console.log("");
if (failures.length) {
  console.log(`\x1b[31m${failures.length} failed\x1b[0m`);
  for (const f of failures) console.log("  - " + f);
  process.exit(1);
}
console.log("\x1b[32mthe ENV tab renders, filters and keeps the reveal to the served build\x1b[0m");
