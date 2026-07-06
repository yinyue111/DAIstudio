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
  /String\(src\)\.startsWith\("blob:"\)\s*\|\|\s*protectedSrc\s*\|\|\s*isPlatformSrc\(src\)/,
  "reference video preview may use local upload blob URLs",
);
assert.match(
  previewSource,
  /const\s+videoSrc\s*=\s*canRenderVideo\s*\?\s*src\s*:\s*""/,
  "reference video preview should only use authenticated, local-upload, or same-origin object URLs",
);
assert.match(
  previewSource,
  /protectedSrc\s*&&\s*!secureSrc/,
  "protected upload thumbnails should wait for authenticated blob URLs before rendering",
);
assert.match(
  previewSource,
  /if\s*\(posterSrc\)/,
  "third-party video references should fall back to poster thumbnails instead of direct playback",
);
assert.match(
  previewSource,
  /authenticatedObjectUrl\(videoPosterSrc\)/,
  "protected upload video posters should load through authenticated blob URLs",
);

console.log("reference preview security test passed");
