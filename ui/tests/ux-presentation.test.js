const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");

global.window = global;
global.document = {
  documentElement: {
    dataset: { locale: "en" },
    lang: "en",
    style: { setProperty: () => {} },
  },
  addEventListener: () => {},
  querySelectorAll: () => [],
};
global.FwrouterUI = {
  escapeHtml: (value) => String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;"),
  translateBackendMessage: (value) => String(value || ""),
};

function loadScript(relativePath) {
  const source = fs.readFileSync(path.join(root, relativePath), "utf8");
  vm.runInThisContext(source, { filename: relativePath });
}

loadScript("static/js/fwrouter-i18n.js");
loadScript("static/js/fwrouter-labels.js");
loadScript("static/js/fwrouter-settings-events.js");
loadScript("static/js/fwrouter-settings-journal.js");

const events = global.FwrouterSettingsEvents;
const journal = global.FwrouterSettingsJournal;

const event = events.toTypedEvent({
  event_id: "evt-1",
  timestamp: "2026-09-04T00:00:00Z",
  severity: "error",
  event_type: "runtime_failed",
  entity_type: "xray",
  entity_id: "xray:alice",
  request_id: "req-1",
  job_id: "job-1",
  apply_id: "apply-1",
  message: "Xray binding failed",
  details: {
    error_code: "SCOPED_RUNTIME_PENDING_INACTIVE_SUBJECT",
    implementation: "xray",
  },
}, "operational");

const rows = journal.renderEventsHtml([event], 0, () => 0);
assert.match(rows, /External client connection unavailable/);
assert.doesNotMatch(rows, /SCOPED_RUNTIME_PENDING_INACTIVE_SUBJECT/);
assert.doesNotMatch(rows, /Xray binding failed/);

const context = journal.renderSelectedEventContextHtml(event);
assert.match(context, /Recommended action/);
assert.match(context, /Technical details/);
assert.match(context, /SCOPED_RUNTIME_PENDING_INACTIVE_SUBJECT/);
assert.match(context, /apply-1/);

console.log("fwrouter UX presentation contract ok");
