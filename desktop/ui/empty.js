(async () => {
  const { invoke, t, localize } = window.obs;
  await localize();
  const status = document.getElementById("status"), build = document.getElementById("build");
  build.onclick = async () => {
    build.disabled = true; status.className = "status"; status.textContent = t("Building the dashboard…");
    try { await invoke("build_dashboard"); }
    catch (e) { status.className = "status bad"; status.textContent = t("Could not build the dashboard") + " (" + (e.code || e) + ")"; build.disabled = false; }
  };
  document.getElementById("settings").onclick = () => invoke("open_settings");
})();
