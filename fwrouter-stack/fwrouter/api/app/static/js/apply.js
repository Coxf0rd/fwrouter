let lastPlan = null;

async function applyDryRun() {
  const r = await fetch("/api/apply/dry-run", { method: "POST" });
  const j = await r.json();
  if (!j.ok) throw new Error(JSON.stringify(j));
  lastPlan = j.plan;
  document.getElementById("applyPlan").textContent = j.plan;
  document.getElementById("applyDiff").textContent = j.diff || "";
}

async function applyApply() {
  if (!lastPlan) throw new Error("No plan. Run Dry-run first.");
  const token = document.getElementById("adminToken").value;
  const r = await fetch("/api/apply/apply", {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-fwrouter-admin": token },
    body: JSON.stringify({ plan: lastPlan }),
  });
  const j = await r.json();
  if (!j.ok) throw new Error(JSON.stringify(j));
}

async function applyRollback() {
  const token = document.getElementById("adminToken").value;
  const r = await fetch("/api/apply/rollback", {
    method: "POST",
    headers: { "x-fwrouter-admin": token },
  });
  const j = await r.json();
  if (!j.ok) throw new Error(JSON.stringify(j));
}

window.fwApply = { applyDryRun, applyApply, applyRollback };

function setApplyState(s) {
  const el = document.getElementById("applyState");
  if (el) el.textContent = s || "";
}

window.addEventListener("DOMContentLoaded", () => {
  const btnDry = document.getElementById("applyDryRun");
  const btnApply = document.getElementById("applyDoApply");
  const btnRb = document.getElementById("applyRollback");

  if (btnDry) btnDry.addEventListener("click", async () => {
    setApplyState("planning...");
    try { await window.fwApply.applyDryRun(); setApplyState("plan ready"); }
    catch (e) { setApplyState("error: " + e.message); }
  });

  if (btnApply) btnApply.addEventListener("click", async () => {
    setApplyState("applying...");
    try { await window.fwApply.applyApply(); setApplyState("applied"); }
    catch (e) { setApplyState("error: " + e.message); }
  });

  if (btnRb) btnRb.addEventListener("click", async () => {
    setApplyState("rolling back...");
    try { await window.fwApply.applyRollback(); setApplyState("rollback done"); }
    catch (e) { setApplyState("error: " + e.message); }
  });
});
