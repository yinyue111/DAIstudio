import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(root, "lib/api.js"), "utf8");

const match = source.match(/function resolveApiBase\(\) \{([\s\S]*?)\n\}\n\nexport const API_BASE/);
assert.ok(match, "resolveApiBase helper is missing");
const loginPathMatch = source.match(/export function loginPath\(([^)]*)\) \{([\s\S]*?)\n\}\n\nfunction redirectToLogin/);
assert.ok(loginPathMatch, "loginPath helper is missing");

function apiBase({ configured, href }) {
  const process = { env: { NEXT_PUBLIC_API_BASE: configured, NODE_ENV: "development" } };
  const window = { location: new URL(href) };
  return new Function("process", "window", `function resolveApiBase() {${match[1]}\n}\nreturn resolveApiBase();`)(process, window);
}

assert.equal(
  apiBase({
    configured: "http://127.0.0.1:8000",
    href: "http://localhost:3002/login",
  }),
  "http://localhost:8000",
);
assert.equal(
  apiBase({
    configured: "http://localhost:8000",
    href: "http://127.0.0.1:3002/login",
  }),
  "http://127.0.0.1:8000",
);
assert.equal(
  apiBase({
    configured: "http://localhost:8000",
    href: "http://192.168.31.20:3002/login",
  }),
  "http://192.168.31.20:8000",
  "LAN/mobile access must not call the client device's own localhost",
);
assert.equal(
  apiBase({
    configured: "https://dream.aiwuq.cn",
    href: "https://dream.aiwuq.cn/login",
  }),
  "https://dream.aiwuq.cn",
);
assert.equal(
  apiBase({
    configured: "",
    href: "http://localhost:3002/login",
  }),
  "",
);

{
  const process = { env: { NEXT_PUBLIC_API_BASE: "http://localhost:8000", NODE_ENV: "production" } };
  const window = { location: new URL("http://192.168.31.20:3002/login") };
  const productionApiBase = new Function("process", "window", `function resolveApiBase() {${match[1]}\n}\nreturn resolveApiBase();`)(process, window);
  assert.equal(
    productionApiBase,
    "http://localhost:8000",
    "production builds should not silently rewrite loopback API origins to LAN hosts unless explicitly enabled",
  );
}

{
  const process = {
    env: {
      NEXT_PUBLIC_API_BASE: "http://localhost:8000",
      NODE_ENV: "production",
      NEXT_PUBLIC_ALLOW_LAN_API_ALIAS: "true",
    },
  };
  const window = { location: new URL("http://192.168.31.20:3002/login") };
  const productionLanApiBase = new Function("process", "window", `function resolveApiBase() {${match[1]}\n}\nreturn resolveApiBase();`)(process, window);
  assert.equal(
    productionLanApiBase,
    "http://192.168.31.20:8000",
    "explicit LAN alias opt-in should preserve mobile/local-network testing",
  );
}

function loginPath({ href, nextPath }) {
  const window = { location: new URL(href) };
  return new Function("window", `function loginPath(${loginPathMatch[1]}) {${loginPathMatch[2]}\n}\nreturn loginPath(arguments[1]);`)(window, nextPath);
}

assert.equal(
  loginPath({ href: "http://localhost:3002/profile?filter=image#top" }),
  "/login?next=%2Fprofile%3Ffilter%3Dimage%23top",
);
assert.equal(
  loginPath({ href: "http://localhost:3002/login?next=%2Fprofile" }),
  "/login",
);
assert.equal(
  loginPath({ href: "http://localhost:3002/", nextPath: "//evil.example/path" }),
  "/login",
);

console.log("api base localhost alignment test passed");
