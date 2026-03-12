async function fetchJson(url, opts) {
  const r = await fetch(url, opts || {});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((j.detail || j.error || (r.status + " " + r.statusText)));
  return j;
}

function setText(id, txt) {
  const el = document.getElementById(id);
  if (el) el.textContent = txt || "";
}

function setOut(obj) {
  const el = document.getElementById("mihomoOut");
  if (!el) return;
  el.textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
}

async function mihomoStatus() {
  setText("mihomoState", "loading…");
  try {
    const j = await fetchJson("/api/mihomo/status");
    setText("mihomoState", "ok");
    setOut(j);
  } catch (e) {
    setText("mihomoState", "error: " + e.message);
    setOut({ ok: false, error: e.message });
  }
}

async function mihomoUpdate() {
  setText("mihomoState", "updating…");
  try {
    const j = await fetchJson("/api/mihomo/update", { method: "POST" });
    setText("mihomoState", "updated");
    setOut(j);
    // refresh snapshot once after update
    await mihomoStatus();
  } catch (e) {
    setText("mihomoState", "error: " + e.message);
    setOut({ ok: false, error: e.message });
  }
}

function wire() {
  const btnS = document.getElementById("mihomoStatus");
  const btnU = document.getElementById("mihomoUpdate");
  if (btnS) btnS.addEventListener("click", mihomoStatus);
  if (btnU) btnU.addEventListener("click", mihomoUpdate);

  // SSE: our backend emits `event: update` with JSON payload {type: "..."}
  try {
    const es = new EventSource("/events");
    es.addEventListener("update", (ev) => {
      try {
        const msg = JSON.parse(ev.data || "{}");
        if (msg.type === "mihomo" || msg.type === "mihomo_error") {
          // event-driven refresh; no polling timers
          mihomoStatus();
        }
      } catch (_) {}
    });
  } catch (_) {}

  // initial one-time load (still no polling)
  mihomoStatus();
}

window.addEventListener("DOMContentLoaded", wire);
