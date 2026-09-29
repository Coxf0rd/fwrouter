const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

global.window = global;
vm.runInThisContext(fs.readFileSync(path.join(__dirname, "../static/js/fwrouter-settings-lazy-read.js"), "utf8"));

async function main() {
  let calls = 0;
  const cache = global.FwrouterSettingsLazyRead.createLazyReadCache({ limit: 2, ttlMs: 60000 });
  assert.strictEqual(calls, 0, "creating the cache must not issue a request");
  let resolveRead;
  const loader = () => {
    calls += 1;
    return new Promise((resolve) => { resolveRead = resolve; });
  };
  const first = cache.read("event-1", loader);
  const second = cache.read("event-1", loader);
  assert.strictEqual(calls, 0, "loader starts on the next microtask after explicit disclosure read");
  await Promise.resolve();
  assert.strictEqual(calls, 1, "concurrent disclosure reads share one request");
  resolveRead({ found: true, event: { event_id: "event-1" } });
  assert.deepStrictEqual(await first, await second);
  assert.deepStrictEqual(await cache.read("event-1", loader), { found: true, event: { event_id: "event-1" } });
  assert.strictEqual(calls, 1, "fresh result is served from cache");
  await cache.read("event-2", async () => ({ id: 2 }));
  await cache.read("event-3", async () => ({ id: 3 }));
  assert.strictEqual(cache.peek("event-1"), undefined, "oldest detail is evicted at the configured bound");
  let failedCalls = 0;
  await assert.rejects(cache.read("missing", async () => { failedCalls += 1; throw new Error("gone"); }));
  assert.strictEqual(cache.peek("missing").promise, null, "failed detail request releases its pending slot");
  cache.peek("missing").error = true;
  assert.strictEqual(cache.shouldReadOnDisclosure("missing"), false, "failed disclosure does not auto retry when the open panel is rendered again");
  assert.strictEqual(failedCalls, 1, "a failed disclosure makes one request");
  await cache.read("missing", async () => { failedCalls += 1; return { retry: true }; }, true);
  assert.strictEqual(failedCalls, 2, "retry is a separate explicit request");
  console.log("settings lazy reads defer, deduplicate, cache, and bound requests");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
