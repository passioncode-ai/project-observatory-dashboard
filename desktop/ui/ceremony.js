// The pairing window (NODE slice 5, OBS-197; portable scenario OSS-94): the screen the engine answers for this
// machine's pairing session (`full assistant ceremony`, node/ceremony.py), drawn as it is. The window decides no
// ceremony rule: the code, the wait, the refusal and its reason are the engine's. While the hub does not run pairing,
// the engine says so and this window says so: never a pretend wait, never a pretend success.
(async () => {
  const { invoke, t, localize } = window.obs;
  await localize();
  const $ = id => document.getElementById(id);
  const step = (op, role) => invoke("ceremony", role ? { op, role } : { op });

  const reasons = {
    "codes-differ": t("You said the codes differ. Nothing was added. Check for anyone between the machines, then start again on both."),
    "aborted": t("The other machine stopped the pairing. Nothing was added."),
    "mismatch": t("A message did not match what its machine had promised, so the pairing stopped. Nothing was added; start again on both machines."),
    "other-estate": t("The other machine belongs to another network. Nothing was added."),
    "not-listed": t("The network's new revision does not list this machine with the keys it showed. Nothing was added."),
    "trusted-not-listed": t("The network's new revision no longer lists the machine you paired with. Nothing was added."),
  };
  const refusals = {
    "hub-not-ready": t("Your passkey signs on the hub's page, which is not live yet. Nothing has been signed."),
    "device-keys-refused": t("This machine's keys could not be read or created. Run `project-observatory full node --init` in a terminal to see why."),
    "ceremony-in-progress": t("A pairing is already under way on this machine."),
  };
  const explain = e => refusals[e && e.code] || `${t("The engine stopped with an error")} (${(e && e.code) || "?"})`;
  const say = (text, bad = false) => { $("status").textContent = text; $("status").className = "status" + (bad ? " bad" : ""); };
  const show = (id, text) => { $(id).hidden = !text; $(id).textContent = text || ""; };

  function button(label, op, primary = false, role = undefined, disabled = false) {
    const b = document.createElement("button");
    b.textContent = label;
    if (primary) b.className = "primary";
    b.disabled = disabled;
    b.onclick = () => run(op, role);
    $("actions").append(b);
  }

  function draw(screen) {
    $("actions").replaceChildren();
    show("code", ""); show("fingerprint", ""); show("note", ""); show("reason", "");
    $("keys").replaceChildren(); $("keys").hidden = true;
    const print = screen.fingerprint ? t("Network fingerprint: {fingerprint}", { fingerprint: screen.fingerprint }) : "";
    const seconds = screen.secondsLeft ?? 0;
    let title = t("Pair this machine"), body = "";
    switch (screen.state) {
      case "not-paired":
        body = t("Add this machine to your network. Both machines will show a six-digit code, and you check that they match.");
        button(t("Start pairing"), "start", true, "new");
        break;
      case "hub-unreachable":
        title = t("Waiting for the hub");
        body = screen.hub === "not-ready"
          ? t("The network's hub does not run pairing yet. Nothing was sent and no machine was added. Cancel, and pair again once the hub is ready.")
          : t("The network's hub did not answer. Nothing was sent and no machine was added. Check the connection, then try again.");
        button(t("Cancel pairing"), "cancel");
        break;
      case "waiting-for-hub":
        if (screen.step === "revision") {
          title = t("Waiting for the new revision");
          body = t("The codes match. The machine already in your network now signs a revision that lists this one; it arrives through the hub. {seconds} s left.", { seconds });
        } else {
          title = t("Waiting for the other machine");
          body = t("Open pairing on a machine that is already in your network. This one waits {seconds} s more.", { seconds });
        }
        button(t("Cancel pairing"), "cancel");
        break;
      case "code-shown":
        title = t("Compare the codes");
        body = t("Both screens must show the same six digits. If they differ, someone may be in between: stop the pairing.");
        show("code", screen.code); show("fingerprint", print);
        button(t("The codes match"), "confirm", true);
        button(t("They differ"), "differ");
        break;
      case "passkey":
        title = t("Sign with your passkey");
        body = t("Your passkey signs a revision that adds {machine} to your network. Check that these keys are the new machine's before you sign:", { machine: screen.machine });
        for (const key of screen.keys || []) {
          const li = document.createElement("li"); li.textContent = `${key.kid}  ${key.fingerprint}`; $("keys").append(li);
        }
        $("keys").hidden = !(screen.keys || []).length;
        show("fingerprint", print);
        if (!screen.canSign) show("note", t("Your passkey signs on the hub's page, which is not live yet. Nothing has been signed."));
        button(t("Sign with passkey"), "sign", true, undefined, !screen.canSign);
        button(t("Cancel pairing"), "cancel");
        break;
      case "confirmed":
        title = t("Pairing complete");
        body = t("Your network now lists {machine} with its keys.", { machine: screen.machine });
        show("fingerprint", print);
        break;
      case "refused":
        title = t("Pairing stopped");
        body = reasons[screen.group] || reasons.mismatch;
        show("reason", t("Reason: {reason}", { reason: screen.reason }));
        button(t("Start again"), "cancel", true);
        break;
      case "expired":
        title = t("The pairing expired");
        body = t("Ten minutes passed before both machines finished. Nothing was added; start again on both machines.");
        button(t("Start again"), "cancel", true);
        break;
      default:
        body = `${t("The engine stopped with an error")} (${screen.state})`;
    }
    $("title").textContent = title;
    $("body").textContent = body;
  }

  let busy = false;
  async function run(op, role) {
    if (busy) return;
    busy = true;
    try {
      const answer = await step(op, role);
      if (op !== "status") say("");
      draw(answer.screen);
    } catch (e) {
      say(explain(e), true);
    } finally {
      busy = false;
    }
  }

  await run("status");
  // The wait and the code's time left are the engine's to count; read them again while a window shows them.
  setInterval(() => { if (!document.hidden) run("status"); }, 5000);
})();
