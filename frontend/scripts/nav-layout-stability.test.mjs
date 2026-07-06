import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(root, "components/Nav.jsx"), "utf8");

assert.match(
  source,
  /import Link from "next\/link"/,
  "top navigation should use Next Link for smooth client-side page switching",
);
assert.doesNotMatch(
  source,
  /<a\s+href=/,
  "internal top navigation should not use raw anchors that force a full page reload",
);
assert.match(
  source,
  /const DESKTOP_LINKS = \[\.\.\.LINKS, \["admin", "管理后台", "\/admin"\]\]/,
  "desktop navigation should reserve the admin tab slot before user data finishes loading",
);
assert.match(
  source,
  /const LINK_WIDTH = \{/,
  "desktop navigation links should have stable width slots",
);
assert.match(
  source,
  /invisible pointer-events-none/,
  "reserved admin slot should stay in layout without being interactive for non-admin users",
);
assert.match(
  source,
  /transition-colors/,
  "active navigation state should animate colors without layout-affecting transitions",
);
assert.match(
  source,
  /min-w-\[274px\]/,
  "desktop account area should reserve width while the user profile loads",
);
assert.match(
  source,
  /w-\[176px\]/,
  "credit pill should keep a stable width across balance updates",
);

console.log("nav layout stability test passed");
