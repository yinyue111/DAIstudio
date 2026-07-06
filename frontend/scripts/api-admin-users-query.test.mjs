import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(root, "lib/api.js"), "utf8");

const match = source.match(
  /adminUsers:\s*\(\{ q = "", status = "", is_admin = "", limit = 50, offset = 0 \} = \{\}\) => \{([\s\S]*?)\n  \},\n  adminGrant:/,
);
assert.ok(match, "adminUsers helper body is missing");

function adminUsersPath(args) {
  const request = (path) => path;
  return new Function(
    "request",
    `return (({ q = "", status = "", is_admin = "", limit = 50, offset = 0 } = {}) => {${match[1]}\n  })(arguments[1]);`,
  )(request, args);
}

assert.equal(
  adminUsersPath({ is_admin: false }),
  "/api/admin/users?limit=50&offset=0&is_admin=false",
);
assert.equal(
  adminUsersPath({ is_admin: "false" }),
  "/api/admin/users?limit=50&offset=0&is_admin=false",
);
assert.equal(
  adminUsersPath({ is_admin: "true", q: "138", status: "active", limit: 20, offset: 40 }),
  "/api/admin/users?limit=20&offset=40&q=138&status=active&is_admin=true",
);

console.log("api admin users query test passed");
