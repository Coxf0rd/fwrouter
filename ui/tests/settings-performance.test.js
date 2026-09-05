const assert = require("assert");
const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(path.resolve(__dirname, "../static/js/settings.js"), "utf8");

assert.match(
  source,
  /loadSettingsInventory\(\{\s*background:\s*true\s*\}\)\.catch\(\(\)\s*=>\s*\{\}\);/,
  "Settings workspace should start inventory refresh in the background.",
);
assert.doesNotMatch(
  source,
  /await\s+Promise\.allSettled\(followUps\)/,
  "Settings workspace must not block useful render on inventory follow-ups.",
);
assert.match(
  source,
  /if\s*\(\s*settingsInventoryAbortController\s*\)\s*\{\s*settingsInventoryAbortController\.abort\(\);/s,
  "Switching inventory tabs should cancel stale in-flight inventory requests.",
);
assert.match(
  source,
  /live_observations=\$\{includeLiveObservations\s*\?\s*"true"\s*:\s*"false"\}/,
  "Inventory requests should be able to skip blocking live observations on first load.",
);
assert.match(
  source,
  /const\s+summaryOnly\s*=\s*!opts\.force\s*&&\s*!cacheEntry\.payload;/,
  "Rules first view should render from summary before policy details refresh.",
);
assert.match(
  source,
  /rulesSummary:\s*rules,[\s\S]*subjects:\s*\{\s*items:\s*\[\]\s*\}/,
  "Rules summary-only payload should still render source/group rows.",
);

console.log("fwrouter settings performance UI contract ok");
