import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const previewSource = readFileSync(join(root, "app/studio/StudioMedia.jsx"), "utf8");

assert.doesNotMatch(
  readFileSync(join(root, "app/page.jsx"), "utf8"),
  /rightsConfirmed|合法使用权/,
  "reference rights confirmation UI and generation blocker should be removed",
);
assert.doesNotMatch(
  previewSource,
  /const\s+videoSrc\s*=\s*secureSrc\s*\|\|\s*asset\.display_url\s*\|\|\s*asset\.url/,
  "reference video preview must not direct-load third-party video URLs",
);
assert.match(
  previewSource,
  /const\s+videoSrc\s*=\s*secureSrc\s*\|\|\s*""/,
  "reference video preview should only use authenticated local object URLs",
);
assert.match(
  previewSource,
  /protectedSrc\s*&&\s*!secureSrc/,
  "protected upload thumbnails should wait for authenticated blob URLs before rendering",
);

console.log("reference preview security test passed");
