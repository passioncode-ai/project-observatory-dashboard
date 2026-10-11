// The assistant window (docs/macos/SPEC.md "Assistant window"; SCN-002–004): conversations kept by
// the engine, a question at a time, answers with their evidence, Stop that cancels the job.
(async () => {
  const { invoke, t, localize } = window.obs;
  await localize();
  const $ = id => document.getElementById(id);
  const call = (action, input = {}) => invoke("assistant", { action, input });
  let selected = null, polling = null, draftRequestId = null, generation = 0;

  const messages = {
    "agent-disabled": t("The assistant is switched off: run `project-observatory full configure features agent true`."),
    "provider-unconfigured": t("No model provider is configured for this workspace."),
    "model-unconfigured": t("Choose a model chain: `project-observatory full configure model chain MODEL_ID`."),
    "budget-unset": t("Set a spending ceiling first: `project-observatory full configure budget daily_ceiling 0.50`."),
    "budget-reached": t("The spending ceiling is reached; the assistant waits until it resets."),
    "assistant-busy": t("The assistant is answering another question; try again when it finishes."),
    "disk-full": t("The disk is full; free some space before asking."),
    "credential-shaped-input": t("The question looks like it contains a key; remove it and send again."),
  };
  const explain = e => (messages[e && e.code] || t("The engine stopped with an error")) + (e && e.code && !messages[e.code] ? ` (${e.code})` : "");
  const say = (text, bad = false) => { $("status").textContent = text; $("status").className = "status" + (bad ? " bad" : ""); };
  const requestId = () => draftRequestId || (draftRequestId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9_-]/g, ""));

  async function loadStatus() {
    try {
      const s = await call("status");
      const select = $("project");
      for (const p of s.projects || []) {
        const o = document.createElement("option"); o.value = p.id; o.textContent = p.name; select.append(o);
      }
    } catch (e) { say(explain(e), true); }
  }

  async function loadList() {
    const mine = generation;
    try {
      const { conversations } = await call("list");
      if (mine !== generation) return;
      const ul = $("conversations"); ul.replaceChildren();
      for (const c of conversations) {
        const li = document.createElement("li");
        li.setAttribute("role", "option"); li.tabIndex = 0;
        li.setAttribute("aria-selected", String(c.id === selected));
        li.textContent = c.title || c.id;
        const small = document.createElement("small"); small.textContent = c.updated_at || ""; li.append(small);
        li.onclick = li.onkeydown = ev => { if (ev.type === "click" || ev.key === "Enter") open(c.id); };
        ul.append(li);
      }
      if (!selected) showEmpty(true);
    } catch (e) { say(explain(e), true); }
  }

  function showEmpty(on) { $("empty").hidden = !on; $("transcript").hidden = on; }

  function render(conv) {
    const box = $("transcript"); box.replaceChildren(); showEmpty(false);
    let open = null;
    for (const turn of conv.turns || []) {
      const div = document.createElement("div"); div.className = "turn";
      const q = document.createElement("div"); q.className = "q"; q.textContent = turn.question; div.append(q);
      if (turn.status === "completed" && turn.answer) {
        const a = document.createElement("div"); a.className = "a"; a.textContent = turn.answer.answer; div.append(a);
        if ((turn.answer.next_steps || []).length) {
          const ul = document.createElement("ul"); ul.className = "steps";
          for (const s of turn.answer.next_steps) { const li = document.createElement("li"); li.textContent = s; ul.append(li); }
          div.append(ul);
        }
        if ((turn.answer.evidence || []).length) {
          const d = document.createElement("details"); const sm = document.createElement("summary");
          sm.textContent = t("Evidence ({n})", { n: turn.answer.evidence.length }); d.append(sm);
          for (const ev of turn.answer.evidence) {
            const p = document.createElement("p"); p.textContent = `${ev.title} — ${ev.source}${ev.measured_at ? " · " + ev.measured_at : ""}`; d.append(p);
          }
          div.append(d);
        }
      } else if (["pending", "working"].includes(turn.status)) {
        const m = document.createElement("div"); m.className = "meta"; m.textContent = t("Working…"); div.append(m); open = turn;
      } else {
        const m = document.createElement("div"); m.className = "meta bad";
        m.textContent = turn.status === "cancelled" ? t("Stopped.") : t("This question did not get an answer ({status}).", { status: turn.status || "?" });
        div.append(m);
      }
      box.append(div);
    }
    box.scrollTop = box.scrollHeight;
    busy(open);
  }

  function busy(turn) {
    $("send").disabled = !!turn; $("stop").hidden = !turn;
    if (turn && !polling) poll(turn.job_id);
  }

  async function open(id) {
    selected = id; const mine = generation;
    try { const conv = await call("get", { id }); if (mine === generation && selected === id) render(conv); }
    catch (e) { say(explain(e), true); }
    loadList();
  }

  function poll(jobId) {
    polling = setInterval(async () => {
      try {
        const { job } = await call("job", { id: jobId });
        if (["completed", "failed", "cancelled", "interrupted"].includes(job.status)) {
          clearInterval(polling); polling = null; if (selected) open(selected);
        }
      } catch (e) { clearInterval(polling); polling = null; say(explain(e), true); }
    }, 1500);
  }

  $("composer").onsubmit = async ev => {
    ev.preventDefault();
    const question = $("question").value.trim();
    if (!question) return;
    say(""); $("send").disabled = true;
    const input = { question, request_id: requestId() };
    if (selected) input.conversation_id = selected;
    if ($("project").value) input.project_id = $("project").value;
    try {
      const r = await call("ask", input);
      draftRequestId = null; $("question").value = "";   // accepted: the next draft gets a new id
      selected = r.conversation_id; await open(selected);
    } catch (e) { say(explain(e), true); $("send").disabled = false; }   // the question stays
  };
  $("stop").onclick = async () => {
    const conv = selected && await call("get", { id: selected });
    const open = conv && (conv.turns || []).find(x => ["pending", "working"].includes(x.status));
    if (open) { try { await call("cancel", { id: open.job_id }); } catch (e) { say(explain(e), true); } }
  };
  $("new").onclick = () => { selected = null; draftRequestId = null; $("transcript").replaceChildren(); showEmpty(true); loadList(); $("question").focus(); };
  $("refresh").onclick = () => selected ? open(selected) : loadList();
  for (const s of [t("What changed on this machine this week?"), t("Which projects have unpushed work?"), t("What should I fix first?")]) {
    const b = document.createElement("button"); b.textContent = s;
    b.onclick = () => { $("question").value = s; $("question").focus(); };
    $("starters").append(b);
  }
  window.__TAURI__.event.listen("workspace-changed", () => { generation++; selected = null; location.reload(); });
  await loadStatus();
  await loadList();
})();
