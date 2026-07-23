import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const apiSource = readFileSync(join(root, "lib/api.js"), "utf8");

assert.match(
  apiSource,
  /a\.download\s*=\s*finalFilename/,
  "downloadBlob must always set anchor.download so blob responses are saved instead of opened in the current tab",
);
assert.match(
  apiSource,
  /document\.body\.appendChild\(a\)/,
  "downloadBlob should attach the temporary anchor before clicking for browser compatibility",
);
assert.match(
  apiSource,
  /fallbackDownloadFilename\(path,\s*res\.headers\.get\("content-type"\)\)/,
  "downloadBlob should derive a safe filename when response headers do not provide one",
);
assert.match(
  apiSource,
  /setTimeout\(\(\)\s*=>\s*\{[\s\S]*URL\.revokeObjectURL\(u\)[\s\S]*a\.remove\(\)/,
  "downloadBlob should clean up the object URL and temporary anchor after the browser starts the download",
);
assert.match(
  apiSource,
  /async function fetchBlobWithTimeout[\s\S]*?fetch\(url, \{ cache: "no-store", \.\.\.options, signal \}\)/,
  "authenticated Blob previews and downloads must bypass the browser cache so repeated cross-origin downloads retain their CORS response",
);

console.log("download blob navigation test passed");
