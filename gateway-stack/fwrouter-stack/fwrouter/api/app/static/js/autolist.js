// autolist.js — manual run, no polling
(function () {
  const el = (id) => document.getElementById(id);

  async function fetchJson(url, opts) {
    const r = await fetch(url, opts || {});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.detail || j.error || (r.status + " " + r.statusText)));
    return j;
  }

  function setText(id, txt) {
    const e = el(id);
    if (e) e.textContent = txt || "";
  }

  function pretty(obj) {
    return typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  }

  function getAdminToken() {
    const t = el("adminToken");
    return (t && t.value) ? t.value : "";
  }

  async function loadStatus(reason) {
    setText("autolistState", reason ? `loading… (${reason})` : "loading…");
    try {
      const j = await fetchJson("/api/autolist/status", { cache: "no-store" });
      setText("autolistState", "ok");
      const out = el("autolistOut");
      if (out) out.textContent = pretty(j);
    } catch (e) {
      setText("autolistState", "error: " + e.message);
    }
  }

  async function runAutolist() {
    const token = getAdminToken();
    setText("autolistState", "running…");
    try {
      const j = await fetchJson("/api/autolist/run", {
        method: "POST",
        headers: { "x-fwrouter-admin": token },
      });
      setText("autolistState", j.ok ? "ok" : "error");
      const out = el("autolistOut");
      if (out) out.textContent = pretty(j);
    } catch (e) {
      setText("autolistState", "error: " + e.message);
    }
  }

  function initSSE() {
    try {
      const es = new EventSource("/events");
      es.addEventListener("update", (ev) => {
        try {
          const msg = JSON.parse(ev.data || "{}");
          if (msg.type === "autolist") {
            loadStatus("sse");
          }
        } catch (_) {}
      });
    } catch (_) {}
  }

  function wire() {
    const btn = el("autolistRun");
    if (btn) btn.addEventListener("click", () => runAutolist());
    const btnStatus = el("autolistRefresh");
    if (btnStatus) btnStatus.addEventListener("click", () => loadStatus("manual"));
    initSSE();
    loadStatus("page-open");
  }

  window.addEventListener("DOMContentLoaded", wire);
})();
