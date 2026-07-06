import assert from "node:assert/strict";
import { imageSizeFor, qualityKeyForSize } from "../app/studio/helpers.ts";

assert.equal(imageSizeFor({ w: 16, h: 9 }, "1k", 2048), "1024x576");
assert.equal(imageSizeFor({ w: 9, h: 16 }, "1k", 2048), "576x1024");
assert.equal(imageSizeFor({ w: 1, h: 1 }, "1k", 2048), "1024x1024");
assert.equal(imageSizeFor({ w: 4, h: 5 }, "1k", 2048), "816x1024");
assert.equal(imageSizeFor({ w: 16, h: 9 }, "2k", 2048), "2048x1152");
assert.equal(imageSizeFor({ w: 9, h: 16 }, "2k", 2048), "1152x2048");
assert.equal(imageSizeFor({ w: 1, h: 1 }, "2k", 2048), "2048x2048");
assert.equal(imageSizeFor({ w: 4, h: 5 }, "2k", 2048), "1632x2048");

for (const size of [
  "1280x720",
  "720x1280",
  "1024x1280",
]) {
  assert.equal(qualityKeyForSize(size), "1k");
}

for (const size of [
  "2048x1152",
  "1152x2048",
  "2048x2048",
  "1632x2048",
]) {
  assert.equal(qualityKeyForSize(size), "2k");
}

console.log("studio image size tests passed");
