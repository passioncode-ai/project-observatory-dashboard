// PB-038: the dashboard's `call()` tells a definite refusal from an uncertain outcome.
//
// Takes `class Uncertain … call()` out of a BUILT page, not a copy of the source,
// and drives it with a stub fetch: a refusal with a reason, a dropped connection,
// a timeout, an unreadable answer to success, and a server fault.
//
//   node tests/action_outcome_check.mjs <built page.html> [locale]
//
// The messages go through the page's own language runtime (the catalog and
// `T`), taken from the same page, in the locale the second argument names.
//
// OSS-12 promises more than the classification: what the BUTTON says and whether
// it can be pressed again. So the page's own click handler, `credAction()`, is
// taken out of the same page and driven with the same answers; the result's
// `buttons` holds, per case, the label, `disabled` while the call is pending and
// after the outcome, the hover title and the toast.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(process.argv[2], "utf8");
const m = html.match(/class Uncertain extends Error \{\}[\s\S]*?\nasync function call\([\s\S]*?\n\}\n/);
if (!m) { console.log(JSON.stringify({ error: "call() not found in the page" })); process.exit(1); }
const lang = html.match(/const I18N = [\s\S]*?\nfunction T\([\s\S]*?\n\}\n/);
if (!lang) { console.log(JSON.stringify({ error: "the language runtime was not found in the page" })); process.exit(1); }
const locale = process.argv[3] || "en";

const results = {};
async function run(name, fetchImpl, ms) {
  const ctx = vm.createContext({
    fetch: fetchImpl, AbortController, setTimeout, clearTimeout, JSON, Math, Error, Number, Object, Intl, String,
    TOKEN: "t", localStorage: { getItem: () => locale },
    document: { body: { dataset: { page: "creds" } },
                documentElement: { getAttribute: () => "en" } },
  });
  vm.runInContext(lang[0] + m[0] + "\nthis.call = call; this.Uncertain = Uncertain;", ctx);
  try {
    await ctx.call("probe", {}, ms);
    results[name] = "ok";
  } catch (e) {
    results[name] = (e instanceof ctx.Uncertain ? "uncertain: " : "refused: ") + e.message;
  }
}
const answer = (status, body) => async () => ({ ok: status < 400, status, json: async () => {
  if (body === undefined) throw new SyntaxError("no JSON"); return body; } });

await run("refused", answer(400, { error: "limit must be a number" }));
await run("provider refused", answer(502, { error: "the provider refused: HTTP 401" }));
await run("server fault", answer(500, { error: "action failed" }));
await run("unreadable success", answer(200, undefined));
await run("dropped", async () => { throw new TypeError("network"); });
await run("timeout", (url, opts) => new Promise((_, reject) =>
  opts.signal.addEventListener("abort", () => reject(new Error("aborted")))), 50);
await run("success", answer(200, { ok: true }));

// The click handler, as the page runs it on a "revoke" press.
const handler = html.match(/\nfunction credAction\(btn, c\) \{[\s\S]*?\n\}\n/);
if (!handler) { console.log(JSON.stringify({ ...results, error: "credAction() not found in the page" })); process.exit(1); }
results.buttons = {};
async function press(name, fetchImpl) {
  const toasts = [];
  const btn = { dataset: { act: "revoke" }, disabled: false, textContent: "revoke", title: "" };
  const ctx = vm.createContext({
    fetch: fetchImpl, AbortController, clearTimeout, JSON, Math, Error, Number, Object, Intl, String,
    // The page's 60 s ceiling, elapsed at once: the message still names 60 s.
    setTimeout: (f, ms) => setTimeout(f, Math.min(ms, 20)),
    TOKEN: "t", localStorage: { getItem: () => locale },
    confirm: () => true, prompt: () => null, toast: text => toasts.push(String(text)),
    document: { body: { dataset: { page: "creds" } },
                documentElement: { getAttribute: () => "en" } },
  });
  vm.runInContext(lang[0] + m[0] + handler[0] + "\nthis.credAction = credAction;", ctx);
  ctx.credAction(btn, { id: "cred:x", label: "fixture-key", name: "fixture-key" });
  const pending = { disabled: btn.disabled, label: btn.textContent };
  await new Promise(resolve => setTimeout(resolve, 200));
  results.buttons[name] = { pending, label: btn.textContent, disabled: btn.disabled,
                            title: btn.title, toast: toasts.join(" | ") };
}
await press("refused", answer(400, { error: "limit must be a number" }));
await press("provider refused", answer(502, { error: "the provider refused: HTTP 401" }));
await press("server fault", answer(500, { error: "action failed" }));
await press("unreadable success", answer(200, undefined));
await press("dropped", async () => { throw new TypeError("network"); });
await press("timeout", (url, opts) => new Promise((_, reject) =>
  opts.signal.addEventListener("abort", () => reject(new Error("aborted")))));
await press("success", answer(200, { ok: true }));
console.log(JSON.stringify(results));
