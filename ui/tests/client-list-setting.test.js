"use strict";

const assert = require("node:assert/strict");
const nodeTest = require("node:test");
const { createHash } = require("node:crypto");
const etagFor = label => `"cli1-${createHash("sha256").update(label).digest("hex")}"`;
const { create, legacyDisplayPayload, RESOURCE_PATH } = require("../static/js/fwrouter-client-list-setting.js");

// One stable file wrapper and exact child IDs satisfy the existing strict TAP
// evidence contract; every test below still executes serially and is accounted.
nodeTest("ui/tests/client-list-setting.test.js", async parent => {
const cases = [];
const test = (name, run) => { cases.push([name, run]); };

function response(status, value, etag) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (name) => name.toLowerCase() === "etag" ? etag : null },
    async json() {
      return value === undefined ? {} : { ok: true, data: { client_list: { show_inactive: value, etag } } };
    },
  };
}

test("client-list controller uses conditional API writes and applies only verified owner response", async () => {
  const calls = [];
  const controller = create({
    secureOrigin: true,
    fetch: async (path, options) => {
      calls.push({ path, options });
      if (options.method === "GET") return response(200, false, etagFor("inc-a-r1"));
      return response(200, true, etagFor("inc-a-r2"));
    },
  });

  let state = await controller.observe({ resetDesired: true });
  assert.equal(state.current, false);
  assert.equal(state.desired, false);
  assert.equal(calls[0].path, RESOURCE_PATH);
  assert.equal(calls[0].options.credentials, "omit");
  assert.equal(calls[0].options.headers.Authorization, undefined);

  state = controller.unlock("  bearer-secret-value  ");
  assert.equal(state.credentialPresent, true);
  assert.equal(state.message, "settings.client_list.unverified");
  assert.equal(calls.length, 1, "unlock must not mutate or send the credential");
  controller.setDesired(true);
  state = await controller.save();

  assert.equal(state.current, true);
  assert.equal(state.desired, true);
  assert.equal(state.message, "settings.client_list.saved");
  assert.equal(calls[1].options.method, "PATCH");
  assert.equal(calls[1].options.credentials, "omit");
  assert.equal(calls[1].options.redirect, "error");
  assert.equal(calls[1].options.headers.Authorization, "Bearer bearer-secret-value");
  assert.equal(calls[1].options.headers["If-Match"], etagFor("inc-a-r1"));
  assert.deepEqual(JSON.parse(calls[1].options.body), { show_inactive: true });
});

test("legacy display updates omit the protected field and its incarnation metadata", () => {
  const siblings = { system_visibility: { docker: false }, hidden_subject_ids: ["local:one"] };
  const result = legacyDisplayPayload({
    ...siblings,
    show_inactive: true,
    _show_inactive_contract: { version: 1, incarnation: "private" },
    show_internal_vless: false,
  });
  assert.deepEqual(result, { ...siblings, show_internal_vless: false });
  assert.deepEqual(siblings.system_visibility, { docker: false });
  assert.equal(Object.hasOwn(result, "show_inactive"), false);
  assert.equal(Object.hasOwn(result, "_show_inactive_contract"), false);
});

test("412 displays observed server value, preserves intent, and never retries without another save", async () => {
  const calls = [];
  const controller = create({
    secureOrigin: true,
    fetch: async (_path, options) => {
      calls.push(options);
      if (options.method === "GET") {
        return calls.filter((item) => item.method === "GET").length === 1
          ? response(200, false, etagFor("r1"))
          : response(200, true, etagFor("r2"));
      }
      return calls.filter((item) => item.method === "PATCH").length === 1
        ? response(412)
        : response(200, false, etagFor("r3"));
    },
  });
  await controller.observe({ resetDesired: true });
  controller.unlock("capability");
  controller.setDesired(false);
  let state = await controller.save();

  assert.equal(state.conflict, true);
  assert.equal(state.current, true);
  assert.equal(state.desired, false);
  assert.equal(state.message, "settings.client_list.conflict");
  assert.equal(calls.filter((item) => item.method === "PATCH").length, 1);

  state = await controller.save();
  assert.equal(state.current, false);
  assert.equal(state.desired, false);
  assert.equal(calls.filter((item) => item.method === "PATCH").length, 2);
  assert.equal(calls.at(-1).headers["If-Match"], etagFor("r2"));
});

test("failed GET after 412 cannot turn stale current into an accepted server value", async () => {
  let gets = 0;
  const controller = create({
    secureOrigin: true,
    fetch: async (_path, options) => {
      if (options.method === "PATCH") return response(412);
      gets += 1;
      if (gets === 1) return response(200, false, etagFor("r1"));
      if (gets === 2) throw new Error("temporary observation failure");
      return response(200, true, etagFor("r2"));
    },
  });
  await controller.observe({ resetDesired: true });
  controller.unlock("capability");
  controller.setDesired(true);
  let state = await controller.save();
  assert.equal(state.current, false, "last read is retained for diagnostics only");
  assert.equal(state.loaded, false);
  assert.equal(state.observationConfirmed, false);
  assert.equal(state.conflict, true);
  assert.equal(state.desired, true);

  state = controller.useServerValue();
  assert.equal(state.message, "settings.client_list.refresh_required");
  assert.equal(state.conflict, true);
  assert.equal(state.desired, true);

  state = await controller.observe();
  assert.equal(state.loaded, true);
  assert.equal(state.observationConfirmed, true);
  assert.equal(state.current, true);
  assert.equal(state.desired, true);
  assert.equal(state.conflict, true, "refresh supplies an observation but does not silently resolve the conflict");
  state = controller.useServerValue();
  assert.equal(state.desired, true);
  assert.equal(state.conflict, false);
  assert.equal(state.message, "settings.client_list.server_value_selected");
});

for (const status of [401, 403]) {
  test(`${status} clears capability but preserves the requested value`, async () => {
    const controller = create({
      secureOrigin: true,
      fetch: async (_path, options) => options.method === "GET"
        ? response(200, false, etagFor("r1"))
        : response(status),
    });
    await controller.observe({ resetDesired: true });
    controller.unlock("one-time-capability");
    controller.setDesired(true);
    const state = await controller.save();
    assert.equal(state.credentialPresent, false);
    assert.equal(state.desired, true);
    assert.equal(state.current, false);
  assert.equal(state.message, status === 401
    ? "settings.client_list.credential_rejected"
      : "settings.client_list.forbidden");
  });
}

test("lost PATCH response is observed without treating GET as acknowledgement or resubmitting", async () => {
  let patchCount = 0;
  const controller = create({
    secureOrigin: true,
    fetch: async (_path, options) => {
      if (options.method === "GET") return response(200, true, etagFor("r2"));
      patchCount += 1;
      throw new Error("transport error contains no UI-visible diagnostics");
    },
  });
  await controller.observe({ resetDesired: true });
  controller.unlock("secret-not-for-render");
  controller.setDesired(true);
  const state = await controller.save();
  assert.equal(state.current, true);
  assert.equal(state.desired, true);
  assert.equal(state.uncertain, true);
  assert.equal(state.message, "settings.client_list.outcome_unknown");
  assert.equal(patchCount, 1);
  assert.equal(JSON.stringify(state).includes("secret-not-for-render"), false);
});

test("failed GET after lost PATCH keeps the draft and uncertainty until explicit refresh and choice", async () => {
  let gets = 0;
  let patchCount = 0;
  const controller = create({
    secureOrigin: true,
    fetch: async (_path, options) => {
      if (options.method === "PATCH") {
        patchCount += 1;
        throw new Error("connection closed after request");
      }
      gets += 1;
      if (gets === 1) return response(200, false, etagFor("r1"));
      if (gets === 2) throw new Error("follow-up read unavailable");
      return response(200, true, etagFor("r2"));
    },
  });
  await controller.observe({ resetDesired: true });
  controller.unlock("capability");
  controller.setDesired(true);
  let state = await controller.save();
  assert.equal(state.uncertain, true);
  assert.equal(state.observationConfirmed, false);
  assert.equal(state.loaded, false);
  assert.equal(state.desired, true);
  assert.equal(state.current, false);
  state = controller.useServerValue();
  assert.equal(state.uncertain, true);
  assert.equal(state.desired, true);
  assert.equal(state.message, "settings.client_list.refresh_required");
  assert.equal(patchCount, 1, "failed observation never triggers an automatic retry");

  state = await controller.observe();
  assert.equal(state.current, true);
  assert.equal(state.observationConfirmed, true);
  assert.equal(state.uncertain, true, "fresh read is not causal acknowledgement of the PATCH");
  assert.equal(state.message, "settings.client_list.outcome_unknown");
  state = controller.useServerValue();
  assert.equal(state.desired, true);
  assert.equal(state.uncertain, false);
  assert.equal(state.message, "settings.client_list.server_value_selected");
  assert.equal(patchCount, 1);
});

test("lock clears capability while preserving unsaved intent; insecure origin cannot unlock", async () => {
  const controller = create({
    secureOrigin: true,
    fetch: async (_path, options) => options.method === "GET"
      ? response(200, false, etagFor("r1"))
      : response(200, true, etagFor("r2")),
  });
  await controller.observe({ resetDesired: true });
  controller.unlock("capability");
  controller.setDesired(true);
  let state = controller.lock();
  assert.equal(state.credentialPresent, false);
  assert.equal(state.desired, true);
  state = await controller.save();
  assert.equal(state.current, false);
  assert.equal(state.desired, true);

  const insecure = create({ secureOrigin: false, fetch: async () => response(200, false, etagFor("r1")) });
  assert.equal(insecure.unlock("capability").message, "settings.client_list.https_required");
  assert.equal(insecure.state().credentialPresent, false);
});

test("malformed protected owner response is not presented as a successful save", async () => {
  const controller = create({
    secureOrigin: true,
    fetch: async (_path, options) => options.method === "GET"
      ? response(200, false, etagFor("r1"))
      : { status: 200, ok: true, headers: { get: () => null }, async json() { return {}; } },
  });
  await controller.observe({ resetDesired: true });
  controller.unlock("capability");
  controller.setDesired(true);
  const state = await controller.save();
  assert.equal(state.current, false);
  assert.equal(state.desired, true);
  assert.equal(state.uncertain, true);
  assert.equal(state.message, "settings.client_list.outcome_unknown");
});

test("mounted controls clear the password field, lock on view navigation, and render conflict state", async () => {
  const originalWindow = global.window;
  const originalDocument = global.document;
  const listeners = () => ({
    values: new Map(),
    addEventListener(name, callback) { this.values.set(name, callback); },
    fire(name, event = {}) { return this.values.get(name)?.(event); },
  });
  const makeNode = () => Object.assign(listeners(), {
    value: "", checked: false, disabled: false, hidden: false, textContent: "", dataset: {},
  });
  const names = ["root", "credential", "unlock", "lock", "checkbox", "refresh", "save", "useServer", "status", "current"];
  const nodes = Object.fromEntries(names.map((key) => [key, makeNode()]));
  global.document = {
    getElementById(id) { return nodes[names.find((name) => ids[name] === id)]; },
    addEventListener: (...args) => docListeners.addEventListener(...args),
  };
  const docListeners = listeners();
  global.window = Object.assign(listeners(), { location: { protocol: "https:" }, isSecureContext: true });
  const ids = Object.fromEntries(names.map((name) => [name, `node-${name}`]));
  let patchCount = 0;
  let getCount = 0;
  const controller = require("../static/js/fwrouter-client-list-setting.js").mount(ids, {
    t: (key) => key,
    fetch: async (_path, options) => {
      if (options.method === "GET") {
        getCount += 1;
        return getCount === 1 ? response(200, false, etagFor("r1")) : response(200, true, etagFor("r2"));
      }
      patchCount += 1;
      return response(412);
    },
  });
  try {
    assert.ok(controller);
    await new Promise((resolve) => setImmediate(resolve));
    nodes.credential.value = "typed-capability";
    nodes.unlock.fire("click");
    assert.equal(nodes.credential.value, "");
    assert.equal(nodes.root.dataset.authorization, "entered_unverified");
    assert.equal(nodes.checkbox.disabled, false);
    nodes.checkbox.checked = true;
    nodes.checkbox.fire("change");
    nodes.save.fire("click");
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(patchCount, 1);
    assert.equal(nodes.checkbox.checked, true, "operator intent remains visible after 412");
    assert.equal(nodes.current.textContent, "settings.client_list.value_enabled");
    assert.equal(nodes.status.textContent, "settings.client_list.conflict");
    assert.equal(nodes.useServer.hidden, false);

    docListeners.fire("fwrouter:view", { detail: { view: "user" } });
    assert.equal(controller.state().credentialPresent, false);
    assert.equal(nodes.checkbox.disabled, true);
    assert.equal(nodes.root.dataset.authorization, "locked");
  } finally {
    global.window = originalWindow;
    global.document = originalDocument;
  }
});

test("pending save serializes edits, refresh and duplicate saves; lock fences a late reply", async () => {
  let finish;
  let patches = 0;
  let gets = 0;
  let projections = 0;
  const controller = create({secureOrigin: true, onSaved: async () => { projections += 1; }, fetch: async (_path, options) => {
    if (options.method === "GET") { gets += 1; return response(200, false, etagFor("r1")); }
    patches += 1;
    return new Promise(resolve => { finish = resolve; });
  }});
  await controller.observe({resetDesired: true});
  controller.unlock("old-capability");
  controller.setDesired(true);
  const saving = controller.save();
  assert.equal(controller.state().busy, true);
  await controller.observe();
  controller.setDesired(false);
  await controller.save();
  controller.useServerValue();
  assert.equal(controller.state().desired, true);
  assert.equal(patches, 1);
  assert.equal(gets, 1);
  controller.lock();
  controller.unlock("new-capability");
  assert.equal(controller.state().credentialPresent, false);
  finish(response(200, true, etagFor("r2")));
  await saving;
  assert.equal(controller.state().credentialPresent, false);
  assert.equal(controller.state().uncertain, true);
  assert.equal(controller.state().loaded, false);
  assert.equal(controller.state().current, false);
  assert.equal(controller.state().busy, false);
  assert.equal(projections, 0);
});

test("out-of-order GET cannot replace a newer confirmed observation", async () => {
  let finish;
  let gets = 0;
  const controller = create({secureOrigin: true, fetch: async () => {
    gets += 1;
    if (gets === 1) return new Promise(resolve => { finish = resolve; });
    return response(200, true, etagFor("r2"));
  }});
  const old = controller.observe();
  await controller.observe();
  finish(response(200, false, etagFor("r1")));
  await old;
  assert.equal(controller.state().current, true);
  assert.equal(controller.state().etag, etagFor("r2"));
});

test("failed API envelope and malformed ETag cannot acknowledge a PATCH", async () => {
  for (const badEnvelope of [true, false]) {
    let savedCallbacks = 0;
    let gets = 0;
    const controller = create({secureOrigin: true, onSaved: async () => { savedCallbacks += 1; }, fetch: async (_path, options) => {
      if (options.method === "GET") {
        gets += 1;
        if (gets > 1) throw new Error("confirmation unavailable");
        return response(200, false, etagFor("r1"));
      }
      const tag = badEnvelope ? etagFor("r2") : '"arbitrary-etag"';
      const result = response(200, true, tag);
      if (badEnvelope) result.json = async () => ({ok: false, data: {client_list: {show_inactive: true, etag: tag}}});
      return result;
    }});
    await controller.observe({resetDesired: true});
    controller.unlock("synthetic-capability");
    controller.setDesired(true);
    await controller.save();
    assert.equal(controller.state().message, "settings.client_list.outcome_unknown");
    assert.equal(controller.state().uncertain, true);
    assert.equal(controller.state().desired, true);
    assert.equal(controller.state().current, false);
    assert.equal(controller.state().loaded, false);
    assert.equal(savedCallbacks, 0);
  }
});

test("conditional 304 confirms only the same valid entity tag", async () => {
  let gets = 0;
  const controller = create({secureOrigin: true, fetch: async () => {
    gets += 1;
    if (gets === 1) return response(200, false, etagFor("r1"));
    return response(304, undefined, etagFor(gets === 2 ? "r1" : "r2"));
  }});
  await controller.observe();
  await controller.observe();
  assert.equal(controller.state().loaded, true);
  await controller.observe();
  assert.equal(controller.state().loaded, false);
  assert.equal(controller.state().message, "settings.client_list.invalid_response");
});

for (const [name, run] of cases) await parent.test(name, run);
});
