import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const config = require("../tailwind.config.cjs");
const content = config.content || [];

for (const pattern of [
  "./app/**/*.{js,jsx,ts,tsx}",
  "./components/**/*.{js,jsx,ts,tsx}",
  "./hooks/**/*.{js,jsx,ts,tsx}",
  "./lib/**/*.{js,jsx,ts,tsx}",
]) {
  assert.ok(content.includes(pattern), `tailwind content is missing ${pattern}`);
}

console.log("tailwind content TypeScript test passed");
