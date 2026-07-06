import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const config = await import(join(dirname(fileURLToPath(import.meta.url)), "../tailwind.config.cjs"));
const content = config.default?.content || config.content || [];

assert.ok(
  content.some((entry) => /\bts\b/.test(String(entry)) && /\btsx\b/.test(String(entry))),
  "tailwind content must scan TypeScript files so classes in app/studio/*.ts are kept in production",
);

console.log("tailwind content TypeScript test passed");
