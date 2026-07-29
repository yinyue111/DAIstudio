import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readStudioSource } from "./studio-source.mjs";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const previewSource = readFileSync(join(root, "app/studio/StudioMedia.jsx"), "utf8");

assert.doesNotMatch(
  readStudioSource(root),
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
  /const\s+videoSrc\s*=\s*canRenderVideo\s*&&\s*!videoPlaybackFailed\s*\?\s*src\s*:\s*""/,
  "reference video preview should only use allowed sources and should stop replaying a failed video source",
);
assert.match(
  previewSource,
  /protectedSrc\s*&&\s*!secureSrc/,
  "protected upload thumbnails should wait for authenticated blob URLs before rendering",
);
assert.match(
  previewSource,
  /if\s*\(posterSrc\s*&&\s*!posterFailed\)/,
  "third-party video references should fall back to poster thumbnails instead of direct playback",
);
assert.match(
  previewSource,
  /authenticatedObjectUrl\(videoPosterSrc\)/,
  "protected upload video posters should load through authenticated blob URLs",
);
assert.match(
  previewSource,
  /setVideoPlaybackFailed\(true\)/,
  "video decode failures should fall back to the authenticated poster instead of hiding the preview",
);
assert.match(
  previewSource,
  /if\s*\(asset\?\.type\s*===\s*"video"\)\s*setVideoPlaybackFailed\(true\);\s*else\s*setImageFailed\(true\)/,
  "a protected primary video fetch failure should preserve the poster fallback",
);
assert.match(
  previewSource,
  /protectedSrc\s*&&\s*!secureSrc\s*&&\s*!videoPlaybackFailed/,
  "a failed protected primary video request must not leave the poster fallback stuck loading",
);
assert.match(
  readFileSync(join(root, "lib/api.js"), "utf8"),
  /allowedPrefixes:\s*\["\/api\/uploads\/"\]/,
  "authenticated object URL alias normalization must stay restricted to upload paths",
);

console.log("reference preview security test passed");
