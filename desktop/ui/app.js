// The app's own pages: words from the shared dictionary, and the few commands they may call.
const invoke = (cmd, args) => window.__TAURI__.core.invoke(cmd, args);
let words = {};
const t = (english, values = {}) => {
  let text = words[english] || english;
  for (const [k, v] of Object.entries(values)) text = text.replaceAll(`{${k}}`, String(v));
  return text;
};
async function localize() {
  const l = await invoke("l10n");
  words = l.words;
  document.documentElement.lang = l.lang;
  for (const el of document.querySelectorAll("[data-t]")) el.textContent = t(el.dataset.t);
}
const params = new URLSearchParams(location.search);
window.obs = { invoke, t, localize, params };
