(async () => {
  const { invoke, t, localize } = window.obs;
  await localize();
  const $ = id => document.getElementById(id);
  const s = await invoke("get_settings");
  $("program").value = s.program; $("workspace").value = s.workspace; $("language").value = s.language;
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
