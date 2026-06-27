import assert from "node:assert/strict";
import { imageSizeFor, qualityKeyForSize } from "../app/studio/helpers.js";

assert.equal(imageSizeFor({ w: 16, h: 9 }, "4k", 3840), "3840x2160");
assert.equal(imageSizeFor({ w: 9, h: 16 }, "4k", 3840), "2160x3840");
assert.equal(imageSizeFor({ w: 1, h: 1 }, "4k", 3840), "2880x2880");
assert.equal(imageSizeFor({ w: 4, h: 5 }, "4k", 3840), "2576x3216");

for (const size of [
  "3840x2160",
  "2160x3840",
  "2880x2880",
  "2576x3216",
]) {
  assert.equal(qualityKeyForSize(size), "4k");
}

console.log("studio image size tests passed");
