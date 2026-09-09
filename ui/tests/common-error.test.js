const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");

global.window = global;
global.document = {
  addEventListener() {},
  querySelectorAll() {
    return [];
  },
  getElementById() {
    return null;
  },
};
global.FwrouterI18n = {
  t: (key) => key,
  translateBackendMessage: (value) => String(value || ""),
};

function response({ ok = false, status = 400, statusText = "Bad Request", payload, text, contentType = "application/json" }) {
  return {
    ok,
    status,
    statusText,
    headers: {
      get(name) {
        return String(name || "").toLowerCase() === "content-type" ? contentType : "";
      },
    },
    clone() {
      return {
        async json() {
          if (payload instanceof Error) throw payload;
          return payload;
        },
      };
    },
    async json() {
      if (payload instanceof Error) throw payload;
      return payload;
    },
    async text() {
      return text || "";
    },
  };
}

const source = fs.readFileSync(path.join(root, "static/js/fwrouter-common.js"), "utf8");
vm.runInThisContext(source, { filename: "static/js/fwrouter-common.js" });

async function assertApiError(fetchResponse, expected) {
  global.fetch = async () => fetchResponse;
  await assert.rejects(
    global.FwrouterUI.fetchApiV2("/test"),
    (error) => {
      assert.strictEqual(error.message, expected);
      assert.strictEqual(global.FwrouterUI.actionMessage(error), expected);
      return true;
    },
  );
}

(async () => {
  await assertApiError(
    response({ payload: { detail: "simple detail" } }),
    "simple detail",
  );
  await assertApiError(
    response({ payload: { detail: [{ loc: ["body", "email"], msg: "already exists" }] } }),
    "email: already exists",
  );
  await assertApiError(
    response({ payload: { message: "top-level message" } }),
    "top-level message",
  );
  await assertApiError(
    response({ payload: new Error("invalid json"), text: "plain failure", contentType: "text/plain" }),
    "plain failure",
  );

  global.fetch = async () => {
    throw new Error("network down");
  };
  await assert.rejects(
    global.FwrouterUI.fetchApiV2("/test"),
    (error) => {
      assert.strictEqual(error.message, "network down");
      assert.strictEqual(global.FwrouterUI.actionMessage(error), "network down");
      return true;
    },
  );

  global.fetch = async () => response({ ok: true, status: 200, payload: { ok: true, data: { status: "failed", error: { message: "job exploded" } } } });
  await assert.rejects(
    global.FwrouterUI.pollJob("job-1", { timeoutMs: 5, delayMs: 1 }),
    /job exploded|job\.timeout/,
  );

  console.log("fwrouter common API error normalization ok");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
