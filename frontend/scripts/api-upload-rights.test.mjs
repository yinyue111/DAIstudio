import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const apiSource = readFileSync(join(root, "lib/api.js"), "utf8");

assert.doesNotMatch(
  apiSource,
  /rightsConfirmed|rights_confirmed/,
  "upload helpers should no longer send rights confirmation fields",
);
assert.match(
  apiSource,
  /reportAsset:\s*\(assetId,\s*body\)\s*=>\s*[\s\S]*request\(`\/api\/assets\/\$\{assetId\}\/report`,\s*\{\s*method:\s*"POST",\s*body\s*\}\)/,
  "asset reporting API helper is missing",
);
assert.match(
  apiSource,
  /adminHandleAssetReport:\s*\(reportId,\s*body\)\s*=>\s*[\s\S]*request\(`\/api\/admin\/asset-reports\/\$\{reportId\}\/handle`,\s*\{\s*method:\s*"POST",\s*body\s*\}\)/,
  "admin asset report handling API helper is missing",
);

console.log("api upload helper test passed");
