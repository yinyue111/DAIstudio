import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const storage = new Map();
global.window = {
  location: {
    origin: "http://localhost:3000",
    hostname: "localhost",
    href: "http://localhost:3000/",
  },
  localStorage: {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  },
};

const apiSource = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../lib/api.js"), "utf8");
const loginSource = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../app/login/page.jsx"), "utf8");
const navigationSource = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../lib/navigation.js"), "utf8");

function loadApiModule() {
  const executableSource = apiSource
    .replace(/^import .*;\n?/gm, "")
    .replace(/^export \{.*;\n?/gm, "")
    .replace(/^export /gm, "");
  return new Function(
    "loginPath",
    `${executableSource}\nreturn { getToken, setToken, clearToken, api };`,
  )(() => "/login");
}

const { getToken, setToken, clearToken, api } = loadApiModule();

window.localStorage.setItem("token", "legacy-token");
assert.equal(getToken(), null);
assert.equal(window.localStorage.getItem("token"), null);

setToken("new-token-from-login-response");
assert.equal(window.localStorage.getItem("token"), null);

window.localStorage.setItem("token", "another-legacy-token");
clearToken();
assert.equal(window.localStorage.getItem("token"), null);

const workingLocalStorage = window.localStorage;
const storageFailures = [
  {
    name: "localStorage access",
    install() {
      Object.defineProperty(window, "localStorage", {
        configurable: true,
        get() {
          const error = new Error("Access to storage is disabled");
          error.name = "SecurityError";
          throw error;
        },
      });
    },
  },
  {
    name: "localStorage.removeItem",
    install() {
      Object.defineProperty(window, "localStorage", {
        configurable: true,
        value: {
          removeItem() {
            const error = new Error("Storage mutation is disabled");
            error.name = "SecurityError";
            throw error;
          },
        },
      });
    },
  },
];

let cookieRequest;
global.fetch = async (url, options) => {
  cookieRequest = { url, options };
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify({ id: 1 }),
  };
};

for (const failure of storageFailures) {
  failure.install();
  assert.doesNotThrow(() => {
    assert.equal(getToken(), null);
    setToken("ignored-cookie-token");
    clearToken();
  }, `${failure.name} failure must not block HttpOnly cookie auth`);
  cookieRequest = null;
  assert.deepEqual(await api.me(), { id: 1 });
  assert.ok(cookieRequest, `${failure.name} failure must not prevent the cookie request`);
  assert.equal(cookieRequest.options.credentials, "include");
}

Object.defineProperty(window, "localStorage", {
  configurable: true,
  value: workingLocalStorage,
});

const browserWindow = global.window;
delete global.window;
try {
  assert.doesNotThrow(() => {
    assert.equal(getToken(), null);
    setToken("ignored-server-token");
    clearToken();
  }, "token helpers must be safe when window is unavailable during SSR");
} finally {
  global.window = browserWindow;
}

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
assert.match(
  apiSource,
  /redirectOn401 = true/,
  "API request helper should allow callers to opt out of automatic 401 redirects",
);
assert.match(
  apiSource,
  /redirectOn401 && typeof window !== "undefined" && !path\.startsWith\("\/api\/auth"\)/,
  "401 redirects should be gated by redirectOn401",
);
assert.match(
  loginSource,
  /api\.me\(\{\s*redirectOn401:\s*false\s*\}\)/,
  "login page session probe must not redirect on anonymous 401 responses",
);
assert.match(
  loginSource,
  /<form[^>]*method="post"[^>]*onSubmit=\{submit\}/,
  "login form must degrade to POST so credentials are never leaked through URL query params",
);
assert.match(
  loginSource,
  /scrubCredentialQuery\(\)/,
  "login page must scrub accidentally leaked credential query params on hydration",
);
assert.match(
  navigationSource,
  /SENSITIVE_QUERY_KEYS = \["phone", "password", "smsCode", "sms_code", "nickname"\]/,
  "credential query scrubber must cover login/register sensitive fields",
);
assert.match(
  navigationSource,
  /if \(url\.pathname === "\/login"\) return "";/,
  "login redirect helper must not allow next=/login loops after successful auth",
);
assert.match(
  loginSource,
  /await api\.me\(\{\s*redirectOn401:\s*false\s*\}\);\s*\n\s*navigateAfterLogin\(nextPath,\s*router\);/,
  "login submit should verify the cookie session before leaving the login page",
);
assert.match(
  loginSource,
  /window\.location\.assign\(nextPath\);/,
  "login success should use a hard navigation fallback instead of relying only on client router state",
);

console.log("api auth cookie test passed");
