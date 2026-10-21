(async () => {
  const { invoke, t, localize } = window.obs;
  await localize();
  const $ = id => document.getElementById(id);
  const s = await invoke("get_settings");
  $("program").value = s.program; $("workspace").value = s.workspace; $("language").value = s.language;
  // Open at login (SCN-012): on unless the person turned it off; the change is written at once.
  $("login").checked = s.launch_at_login !== false;
  $("login").onchange = async () => {
    const on = $("login").checked, status = $("login-status");
    status.className = "status"; status.textContent = "";
    try {
      await invoke("set_launch_at_login", { on });
      status.className = "status good";
      status.textContent = on ? t("Project Observatory opens at login.") : t("Project Observatory no longer opens at login.");
    } catch (e) {
      $("login").checked = !on;
      status.className = "status bad";
      status.textContent = t("The startup entry could not be changed: {error}", { error: e.detail || e.code || String(e) });
    }
  };
  const dialog = window.__TAURI__.dialog;
  $("pick-program").onclick = async () => { const p = await dialog.open({ multiple: false, directory: false, defaultPath: $("program").value || undefined }); if (p) $("program").value = p; };
  $("pick-workspace").onclick = async () => { const p = await dialog.open({ multiple: false, directory: true, defaultPath: $("workspace").value || undefined }); if (p) $("workspace").value = p; };
  $("save").onclick = async () => {
    const status = $("status"); status.className = "status"; status.textContent = t("Checking…"); $("save").disabled = true;
    try {
      const r = await invoke("save_settings", { settings: { program: $("program").value.trim(), workspace: $("workspace").value.trim(), language: $("language").value } });
      await localize();
      status.className = "status good";
      status.textContent = t("Connected: engine {version}, {projects} projects.", { version: r.engine_version, projects: r.project_count });
    } catch (e) {
      status.className = "status bad";
      status.textContent = (e.code === "program-not-absolute" || e.code === "workspace-not-absolute") ? t("The path must be absolute.") : (e.code || String(e)) + (e.detail ? ": " + e.detail : "");
    } finally { $("save").disabled = false; }
  };
})();
