// Execute the dashboard's own script against a stub DOM.
//
// WHY THIS EXISTS. On 2026-09-05 a findings block was placed above `const chip`
// and every static check still passed: a temporal-dead-zone ReferenceError
// aborted the script, and the page rendered as a header and a footer with no
// table, no tiles and no findings. Nothing in audit_pack.py executes
// JavaScript, so nothing could have seen it.
//
// A regex was tried first and withdrawn: text cannot tell a reference executed
// at load time from one inside a function called later, and it cried wolf on
// the working page. The only honest check for a runtime error is to run it.
//
// AND WHY IT NOW WRITES A RECEIPT. On 2026-09-08 the log held exactly one
// `dashboard SMOKE FAILED` line — 2026-09-08T03:00:54Z, a `stakeText` defined
// inside one function and called from another, mine from two iterations
// earlier. So this file caught the regression unattended and named it, and its
// report reached a log nobody reads: the tick ran smoke and did not REPORT it,
// no receipt, no finding, no failed step. A verdict that outlives its log line
// is what `tools/build_findings.py` can read.
//
// The receipt carries the checked page's OWN hash, because a clean verdict from
// an hour ago says nothing about the build that replaced it — and a stale clean
// is the same silence, differently spelled.
//
// ITS PATH IS THE PAGE'S, not a setting. A first version took it from an env
// var that only the tick set, and the gate — which builds the page and executes
// it too — recorded nothing, so the board would have reported "verified an
// older build" after every gate run: true of the record, useless to a reader,
// and permanent, because the gate runs more often than the tick. Deriving the
// path from the argument means whoever executed a build records that build, a
// test's temporary copy gets a temporary receipt, and there is no second knob
// that can disagree with `paths.DASHBOARD_HTML`.
//
// AND A SLOW RUN IS NOT A BLANK PAGE. On 2026-10-10 00:52 the page's script did not
// finish inside 10 s under a load average near 300, smoke wrote `blank`, and the
// board raised a critical `dashboard.blank` about a page that rendered clean the
// tick before. `vm`'s timeout is wall-clock, so on a timeout this asks how much
// PROCESSOR the script consumed. The page's script costs well under a second; one
// that has used `--cpu-budget-ms` (5 s) and still has not finished is broken, and
// stays `blank` — it would freeze a browser too. One that got less was not given a
// fair chance: it is tried once more with `--patient-ms`, and if that runs out
// before the budget is spent the verdict is `unmeasured` — the board's
// `dashboard.unverified`, with the reason, and no failed step. A SHARE of processor
// time cannot tell these apart: under a load of 300 a spinning script got 22%, the
// same share a slow but finite one gets.
//
//     node dashboard/smoke.js PAGE [--timeout-ms N] [--patient-ms N] [--cpu-budget-ms N]
const fs = require("fs");
const vm = require("vm");
const crypto = require("crypto");

const file = process.argv[2];
// Options come from the command line, never the environment: the receipt and the
// limits are the run's own, so nothing outside the argument list can change them.
function option(name, fallback) {
  const i = process.argv.indexOf(name);
  const v = i > 2 ? Number(process.argv[i + 1]) : NaN;
  return Number.isFinite(v) && v > 0 ? v : fallback;
}
const TIMEOUT_MS = option("--timeout-ms", 10000);
const PATIENT_MS = option("--patient-ms", 60000);
// Processor time after which an unfinished script is broken rather than starved.
const CPU_BUDGET_MS = option("--cpu-budget-ms", 5000);
const html = fs.readFileSync(file, "utf8");
const pageSha = crypto.createHash("sha256").update(html).digest("hex").slice(0, 12);

const RECEIPT = file.replace(/\.html?$/i, "") + ".smoke.json";

function receipt(verdict, extra) {
  const to = RECEIPT;
  const doc = Object.assign({
    ran_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    page: file, page_sha: pageSha, verdict,
  }, extra || {});
  try {
    fs.writeFileSync(to + ".tmp", JSON.stringify(doc, null, 1) + "\n");
    fs.renameSync(to + ".tmp", to);
  } catch (e) {
    // NAMED, never fatal: smoke's job is the verdict, and losing the receipt
    // must not turn a clean page into a failed tick step.
    console.error("smoke: the receipt could not be written — " + e.message);
  }
}
// EVERY script the page carries, in document order, inline and `src` alike —
// which is what a browser executes. The single page holds one inline block; a
// split page holds its own data inline and then loads the shared `app.js`
// beside it, and running only the inline half would report a blank
// page that renders perfectly.
const SCRIPT = /<script(?:\s+src="([^"]+)")?\s*>([\s\S]*?)<\/script>/g;
const parts = [];
for (const m of html.matchAll(SCRIPT)) {
  if (m[1]) {
    const sib = require("path").join(require("path").dirname(file), m[1]);
    if (!fs.existsSync(sib)) {
      console.error("smoke: " + file + " loads " + m[1] + ", which is not beside it");
      receipt("blank", { reason: "the page loads " + m[1] + ", which is not beside it" });
      process.exit(1);
    }
    parts.push(fs.readFileSync(sib, "utf8"));
  } else {
    parts.push(m[2]);
  }
}
if (!parts.length) {
  console.error("smoke: no <script> in " + file);
  receipt("blank", { reason: "the page carries no <script> at all" });
  process.exit(1);
}
const source = parts.join("\n;\n");

function attempt(timeoutMs) {
  const made = [];
  function el(id) {
    const node = {
      id, innerHTML: "", textContent: "", value: "", dataset: {}, children: [],
      style: { setProperty() {} }, classList: { add() {}, remove() {}, toggle: () => false,
        contains: () => false },
      offsetHeight: 56, offsetParent: {},
      setAttribute() {}, getAttribute: () => null, addEventListener() {},
      querySelector: () => el("q"), querySelectorAll: () => [],
      // Findings removes a stale empty-state notice after clearing filters.
      // Real elements provide remove(); omitting it reports a harness defect.
      remove() {},
      closest: () => null, previousElementSibling: el ? null : null,
    };
    made.push(node);
    return node;
  }
  const document = {
    documentElement: { setAttribute() {}, getAttribute() { return null; }, style: { setProperty() {} } },
    getElementById: id => el(id),
    querySelector: () => el("sel"),
    querySelectorAll: () => [],
    addEventListener() {},
  };
  // A stub that lacks what a browser gives is a stub that reports a defect the
  // page does not have. `addEventListener` at global scope and `location` arrived
  // with the addressable project panel (S3): the page listens for `hashchange`
  // and reads `location.hash`, both of which every browser provides and this file
  // did not. Adding them is the stub catching up, not the page bending around it.
  // `search` and `URLSearchParams` arrived with the filtered tile link (T-25):
  // a tile on the overview opens the table already narrowed, which the page
  // reads from the address. Both are things every browser provides.
  const location = { hash: "", search: "", href: "file:///dashboard",
                     assign() {}, replace() {} };
  const context = {
    document,
    location,
    addEventListener() {},
    removeEventListener() {},
    window: { matchMedia: () => ({ matches: false, addEventListener() {} }) },
    ResizeObserver: class { observe() {} },
    URLSearchParams,
    console,
  };
  context.window.document = document;
  context.window.location = location;
  context.window.addEventListener = context.addEventListener;
  context.matchMedia = context.window.matchMedia;
  const cpu0 = process.cpuUsage();
  const wall0 = Date.now();
  try {
    vm.createContext(context);
    vm.runInContext(source, context, { filename: "dashboard-script", timeout: timeoutMs });
    return { made };
  } catch (error) {
    const used = process.cpuUsage(cpu0);
    return { made, error, cpu_ms: Math.round((used.user + used.system) / 1000),
             wall_ms: Date.now() - wall0 };
  }
}

function threw(e) {
  console.error("smoke: the page's script threw at load — the page would render blank");
  const trace = e && e.stack ? e.stack.split("\n").slice(0, 3).join("\n  ") : String(e);
  console.error("  " + trace);
  receipt("blank", {
    reason: "the page's script threw at load: "
            + ((e && e.message) ? `${e.name}: ${e.message}` : String(e)),
  });
  process.exit(1);
}

const timedOut = r => r.error && r.error.code === "ERR_SCRIPT_EXECUTION_TIMEOUT";
const starved = r => r.cpu_ms < CPU_BUDGET_MS;

let run = attempt(TIMEOUT_MS);
if (timedOut(run) && starved(run)) {
  console.error(`smoke: the script got ${run.cpu_ms} ms of processor in ${run.wall_ms} ms — ` +
                `below the ${CPU_BUDGET_MS} ms budget, so the machine is busy; ` +
                `trying once more with ${PATIENT_MS} ms`);
  run = attempt(PATIENT_MS);
}
if (timedOut(run) && starved(run)) {
  console.error(`smoke: not measured — the script got ${run.cpu_ms} ms of processor in ` +
                `${run.wall_ms} ms; a busy machine, not a broken page`);
  receipt("unmeasured", {
    reason: `the machine was too busy to run the page's script: it got ${run.cpu_ms} ms of ` +
            `processor time in ${run.wall_ms} ms, short of the ${CPU_BUDGET_MS} ms a broken ` +
            `script would have used, so the timeout says nothing about the page`,
    cpu_ms: run.cpu_ms, wall_ms: run.wall_ms,
  });
  process.exit(0);
}
if (run.error) threw(run.error);
const made = run.made;

// Rendering must have produced something. A script that runs and writes nothing
// is the same blank page by a different route.
const wrote = made.filter(n => n.innerHTML && n.innerHTML.length > 200);
if (!wrote.length) {
  console.error("smoke: the script ran but wrote no markup — nothing would be visible");
  receipt("blank", {
    reason: "the script ran and wrote no markup — nothing would be visible",
  });
  process.exit(1);
}
const largest = Math.max(...wrote.map(n => n.innerHTML.length));
receipt("clean", { containers: wrote.length, largest });
console.log(`smoke: script ran clean, ${wrote.length} container(s) written, ` +
            `${largest} chars in the largest`);
