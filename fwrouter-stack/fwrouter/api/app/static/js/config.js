// config.js — edit fwrouter configs (manual save, no polling)
(function () {
  const el = (id) => document.getElementById(id);

  async function fetchJson(url, opts) {
    const r = await fetch(url, opts || {});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.detail || j.error || (r.status + " " + r.statusText)));
    return j;
  }

  function getAdminToken() {
    const t = el("adminToken");
    return (t && t.value) ? t.value : "";
  }

  function setText(id, txt) {
    const e = el(id);
    if (e) e.textContent = txt || "";
  }

  async function loadConfigs(reason) {
    setText("configState", reason ? `loading… (${reason})` : "loading…");
    try {
      const j = await fetchJson("/api/config", { cache: "no-store" });
      const files = j.files || {};
      if (el("cfgFwrouter")) el("cfgFwrouter").value = files.fwrouter || "";
      if (el("cfgDevices")) el("cfgDevices").value = files.devices || "";
      if (el("cfgRoutes")) el("cfgRoutes").value = files.routes || "";
      if (el("cfgDomains")) el("cfgDomains").value = files.domains || "";
      if (el("cfgPolicy")) el("cfgPolicy").value = files.policy || "";
      if (el("cfgAutolist")) el("cfgAutolist").value = files.autolist || "";
      setText("configState", "ok");
    } catch (e) {
      setText("configState", "error: " + e.message);
    }
  }

  async function saveConfig(name, textareaId) {
    const token = getAdminToken();
    const content = el(textareaId)?.value || "";
    setText("configState", "saving…");
    try {
      await fetchJson("/api/config", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-fwrouter-admin": token,
        },
        body: JSON.stringify({ name, content }),
      });
      setText("configState", "saved");
    } catch (e) {
      setText("configState", "error: " + e.message);
    }
  }

  function wire() {
    const btnLoad = el("configRefresh");
    if (btnLoad) btnLoad.addEventListener("click", () => loadConfigs("manual"));

    const map = [
      ["saveFwrouter", "fwrouter", "cfgFwrouter"],
      ["saveDevices", "devices", "cfgDevices"],
      ["saveRoutes", "routes", "cfgRoutes"],
      ["saveDomains", "domains", "cfgDomains"],
      ["savePolicy", "policy", "cfgPolicy"],
      ["saveAutolist", "autolist", "cfgAutolist"],
    ];
    map.forEach(([btnId, name, tid]) => {
      const btn = el(btnId);
      if (btn) btn.addEventListener("click", () => saveConfig(name, tid));
    });

    loadConfigs("page-open");
  }

  window.addEventListener("DOMContentLoaded", wire);
})();
