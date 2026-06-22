import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const storage = new Map();
global.window = {
  localStorage: {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  },
};

const apiSource = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../lib/api.js"), "utf8");

function legacyTokenClearingFunction(name) {
  const match = apiSource.match(new RegExp(`export function ${name}\\([^)]*\\) \\{([\\s\\S]*?)\\n\\}`));
  assert.ok(match, `expected ${name} export in frontend/lib/api.js`);
  return new Function(match[1]);
}

const getToken = legacyTokenClearingFunction("getToken");
const setToken = legacyTokenClearingFunction("setToken");
const clearToken = legacyTokenClearingFunction("clearToken");

window.localStorage.setItem("token", "legacy-token");
assert.equal(getToken(), null);
assert.equal(window.localStorage.getItem("token"), null);

setToken("new-token-from-login-response");
assert.equal(window.localStorage.getItem("token"), null);

window.localStorage.setItem("token", "another-legacy-token");
clearToken();
assert.equal(window.localStorage.getItem("token"), null);

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const appDir = join(root, "app");
function pageFiles(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    const stat = statSync(path);
    if (stat.isDirectory()) return pageFiles(path);
    return name.endsWith(".jsx") ? [path] : [];
  });
}
for (const path of pageFiles(appDir)) {
  const source = readFileSync(path, "utf8");
  assert.doesNotMatch(
    source,
    /\bgetToken\s*\(/,
    `${path} must rely on api.me() cookie validation instead of getToken()`,
  );
}

console.log("api auth cookie test passed");
