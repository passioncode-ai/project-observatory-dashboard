(async () => {
  const { invoke, t, localize, params } = window.obs;
  await localize();
  const code = params.get("code") || "", detail = params.get("detail") || "";
  if (code === "starting") {   // the engine is being asked where the dashboard is
    document.getElementById("title").textContent = t("Project Observatory");
    document.querySelector(".actions").hidden = true;
    return;
  }
  const text = {
    "backend-missing": ["Observatory is not installed yet", t("No engine at {path}. Install it as README → Install describes, or choose the program in Settings.", { path: detail })],
    "unknown-workspace": ["This folder is not a workspace", t("Run `project-observatory full init` (with OBSERVATORY_HOME set to it), or choose an existing workspace in Settings.")],
    "backend-incompatible": ["This engine is too old for the app", t("Run `project-observatory full update --apply`, then Retry.") + (detail ? " (" + detail + ")" : "")],
    "backend-timeout": ["The engine did not answer", ""],
    "backend-failed": ["The engine stopped with an error", detail],
  }[code] || ["Something went wrong", code + (detail ? ": " + detail : "")];
  document.getElementById("title").textContent = t(text[0]);
  document.getElementById("detail").textContent = text[1];
  document.getElementById("retry").onclick = () => invoke("show_dashboard_cmd");
  document.getElementById("settings").onclick = () => invoke("open_settings");
  document.getElementById("guide").onclick = () => invoke("open_guide");
})();
