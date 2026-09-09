const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");

function makeNode(id) {
  const classes = new Set();
  return {
    id,
    dataset: {},
    disabled: false,
    textContent: "",
    classList: {
      add(...names) {
        names.forEach((name) => classes.add(name));
      },
      remove(...names) {
        names.forEach((name) => classes.delete(name));
      },
      toggle(name, enabled) {
        if (enabled) classes.add(name);
        else classes.delete(name);
      },
      contains(name) {
        return classes.has(name);
      },
    },
    setAttribute(name, value) {
      this[name] = value;
    },
    hasAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this, name);
    },
    getAttribute(name) {
      return this[name];
    },
    removeAttribute(name) {
      delete this[name];
    },
  };
}

const nodes = {
  settingsExternalClientCreateState: makeNode("settingsExternalClientCreateState"),
  form: makeNode("form"),
  submit: makeNode("submit"),
  alias: makeNode("alias"),
  email: makeNode("email"),
  toggle: makeNode("toggle"),
};

global.window = global;
global.document = {
  addEventListener() {},
  querySelectorAll() {
    return [];
  },
  getElementById(id) {
    return nodes[id] || null;
  },
};
global.setTimeout = () => 0;
global.clearTimeout = () => {};
global.FwrouterI18n = {
  t(key, params) {
    if (key === "status.error_prefix") return `Error: ${params.message}`;
    return key;
  },
  translateBackendMessage(value) {
    return String(value || "");
  },
};

vm.runInThisContext(
  fs.readFileSync(path.join(root, "static/js/fwrouter-common.js"), "utf8"),
  { filename: "static/js/fwrouter-common.js" },
);
vm.runInThisContext(
  fs.readFileSync(path.join(root, "static/js/fwrouter-ui-action.js"), "utf8"),
  { filename: "static/js/fwrouter-ui-action.js" },
);

global.fetch = async (url, options) => {
  assert.strictEqual(url, "/api/v2/xray/clients");
  assert.strictEqual(options.method, "POST");
  return {
    ok: false,
    status: 400,
    statusText: "Bad Request",
    headers: {
      get(name) {
        return String(name || "").toLowerCase() === "content-type" ? "application/json" : "";
      },
    },
    clone() {
      return {
        async json() {
          return { detail: [{ loc: ["body", "email"], msg: "invalid link" }] };
        },
      };
    },
    async text() {
      return "";
    },
  };
};

(async () => {
  nodes.submit.classList.add("is-success-scope");

  await assert.rejects(
    global.FwrouterUIAction.runAction({
      id: "settings.external_client.create",
      button: nodes.submit,
      scope: nodes.form,
      resultTarget: nodes.settingsExternalClientCreateState,
      messageTarget: nodes.settingsExternalClientCreateState,
      disable: [nodes.alias, nodes.email, nodes.submit, nodes.toggle],
      pendingMessage: "status.saving",
      successMessage: "settings.external_client.created",
      failedMessage: "status.error_prefix",
      resultFlashMs: 1,
      resultIconMs: 1,
      action: async () => global.FwrouterUI.fetchApiV2("/xray/clients", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          alias: "broken",
          email: "broken",
          requested_by: "ui",
        }),
      }),
    }),
  );

  assert.strictEqual(nodes.settingsExternalClientCreateState.textContent, "Error: email: invalid link");
  assert.strictEqual(nodes.submit.disabled, false);
  assert.strictEqual(nodes.alias.disabled, false);
  assert.strictEqual(nodes.email.disabled, false);
  assert.strictEqual(nodes.toggle.disabled, false);
  assert.strictEqual(nodes.submit.classList.contains("is-pending"), false);
  assert.strictEqual(nodes.settingsExternalClientCreateState.classList.contains("is-error-scope"), true);
  assert.strictEqual(nodes.settingsExternalClientCreateState.classList.contains("is-success-scope"), false);

  console.log("fwrouter VLESS create error lifecycle contract ok");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
