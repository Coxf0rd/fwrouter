const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");

class ClassList {
  constructor() {
    this.items = new Set();
  }

  add(...names) {
    names.forEach((name) => this.items.add(name));
  }

  remove(...names) {
    names.forEach((name) => this.items.delete(name));
  }

  toggle(name, force) {
    const next = force === undefined ? !this.items.has(name) : Boolean(force);
    if (next) this.items.add(name);
    else this.items.delete(name);
    return next;
  }

  contains(name) {
    return this.items.has(name);
  }
}

class Node {
  constructor(id) {
    this.id = id || "";
    this.dataset = {};
    this.classList = new ClassList();
    this.attributes = new Map();
    this.disabled = false;
    this.textContent = "";
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === "data-result-icon") delete this.dataset.resultIcon;
  }

  getAttribute(name) {
    return this.attributes.get(name) || null;
  }
}

const nodes = new Map();
function node(id) {
  const item = new Node(id);
  nodes.set(id, item);
  return item;
}

global.window = global;
global.document = {
  getElementById: (id) => nodes.get(id) || null,
};
global.FwrouterI18n = {
  t: (key, params) => {
    if (key === "status.error_prefix") return `error:${params?.message || ""}`;
    return key;
  },
};

const calls = {
  failPending: 0,
  pollJob: [],
  messages: [],
};

global.FwrouterUI = {
  setPendingStateMany(targets, pending) {
    calls.failPending += pending ? 0 : 1;
    targets.forEach((target) => {
      if (!target) return;
      target.disabled = Boolean(pending);
      target.classList.toggle("is-pending", Boolean(pending));
    });
  },
  setDynamicStatus(id, key, params) {
    calls.messages.push({ id, key, params });
    const target = nodes.get(id);
    if (target) target.textContent = key;
  },
  setText(id, value) {
    const target = nodes.get(id);
    if (target) target.textContent = value;
  },
  actionMessage(error) {
    return String(error?.message || "");
  },
  translateBackendMessage(value) {
    return String(value || "");
  },
  async pollJob(jobId) {
    calls.pollJob.push(jobId);
    return { job_id: jobId, status: "success" };
  },
};

const source = fs.readFileSync(path.join(root, "static/js/fwrouter-ui-action.js"), "utf8");
vm.runInThisContext(source, { filename: "static/js/fwrouter-ui-action.js" });

const fastResultTimers = {
  resultFlashMs: 1,
  resultIconMs: 1,
};

async function testRunActionCallsAction() {
  const button = node("button-one");
  const message = node("message-one");
  let called = false;
  const result = await global.FwrouterUIAction.runAction({
    id: "test.action",
    button,
    resultTarget: button,
    messageTarget: message,
    pendingMessage: "status.applying",
    successMessage: "status.ok",
    ...fastResultTimers,
    action: async () => {
      called = true;
      return {};
    },
  });

  assert.strictEqual(called, true);
  assert.strictEqual(result.state, "SUCCESS");
}

async function testSuccessClearsPending() {
  const button = node("button-two");
  await global.FwrouterUIAction.runAction({
    button,
    resultTarget: button,
    ...fastResultTimers,
    action: async () => ({}),
  });

  assert.strictEqual(button.disabled, false);
  assert.strictEqual(button.classList.contains("is-pending"), false);
}

async function testErrorUsesFailedState() {
  const button = node("button-three");
  const message = node("message-three");

  await assert.rejects(
    global.FwrouterUIAction.runAction({
      button,
      resultTarget: button,
      messageTarget: message,
      failedMessage: "status.error_prefix",
      ...fastResultTimers,
      action: async () => {
        throw new Error("boom");
      },
    }),
    (error) => error.actionState === "FAILED",
  );

  assert.strictEqual(button.classList.contains("is-error-scope"), true);
  assert.strictEqual(message.textContent, "status.error_prefix");
}

async function testControlsReenableAfterError() {
  const button = node("button-four");
  const input = node("input-four");

  await assert.rejects(global.FwrouterUIAction.runAction({
    button,
    resultTarget: button,
    disable: [input],
    ...fastResultTimers,
    action: async () => {
      throw new Error("boom");
    },
  }));

  assert.strictEqual(button.disabled, false);
  assert.strictEqual(input.disabled, false);
  assert.strictEqual(button.classList.contains("is-pending"), false);
  assert.strictEqual(input.classList.contains("is-pending"), false);
}

async function testTargetsAreExplicitOnly() {
  const button = node("button-five");
  const scope = node("scope-five");
  const sibling = node("sibling-five");

  await global.FwrouterUIAction.runAction({
    button,
    scope,
    resultTarget: button,
    ...fastResultTimers,
    action: async () => ({}),
  });

  assert.strictEqual(button.classList.contains("is-success-scope"), true);
  assert.strictEqual(scope.classList.contains("is-success-scope"), false);
  assert.strictEqual(sibling.classList.contains("is-pending"), false);
  assert.strictEqual(sibling.classList.contains("is-success-scope"), false);
}

async function testFailureClearsPreviousSuccessAndPending() {
  const button = node("button-six");
  const message = node("message-six");

  await global.FwrouterUIAction.runAction({
    button,
    resultTarget: button,
    messageTarget: message,
    successMessage: "status.ok",
    ...fastResultTimers,
    action: async () => ({}),
  });

  assert.strictEqual(button.classList.contains("is-success-scope"), true);

  await assert.rejects(global.FwrouterUIAction.runAction({
    button,
    resultTarget: button,
    messageTarget: message,
    failedMessage: "status.error_prefix",
    ...fastResultTimers,
    action: async () => {
      throw new Error("failed now");
    },
  }));

  assert.strictEqual(button.disabled, false);
  assert.strictEqual(button.classList.contains("is-pending"), false);
  assert.strictEqual(button.classList.contains("is-success-scope"), false);
  assert.strictEqual(button.classList.contains("is-error-scope"), true);
  assert.strictEqual(message.textContent, "status.error_prefix");
}

(async () => {
  await testRunActionCallsAction();
  await testSuccessClearsPending();
  await testErrorUsesFailedState();
  await testControlsReenableAfterError();
  await testTargetsAreExplicitOnly();
  await testFailureClearsPreviousSuccessAndPending();
  console.log("fwrouter UI action lifecycle contract ok");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
