import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

process.env.NODE_ENV = "production";
process.env.NEXT_PUBLIC_API_BASE = "http://127.0.0.1:8000";
const require = createRequire(import.meta.url);
const config = require(join(dirname(fileURLToPath(import.meta.url)), "../next.config.js"));
assert.equal(typeof config.headers, "function");

const routes = await config.headers();
const root = routes.find((route) => route.source === "/:path*");
assert.ok(root, "global security header route is missing");

const headers = new Map(root.headers.map((item) => [item.key.toLowerCase(), item.value]));
assert.match(headers.get("content-security-policy") || "", /default-src 'self'/);
assert.match(
  headers.get("content-security-policy") || "",
  /script-src[^;]*unsafe-inline/,
  "production CSP must allow Next hydration inline scripts",
);
assert.doesNotMatch(
  headers.get("content-security-policy") || "",
  /script-src[^;]*unsafe-eval/,
  "production CSP must not allow unsafe-eval",
);
assert.doesNotMatch(
  headers.get("content-security-policy") || "",
  /connect-src[^;]*\shttps:/,
  "production CSP must not allow arbitrary HTTPS connections",
);
assert.doesNotMatch(
  headers.get("content-security-policy") || "",
  /(?:img-src|media-src)[^;]*\shttps:/,
  "production CSP must not allow arbitrary HTTPS media sources",
);
assert.match(
  headers.get("content-security-policy") || "",
  /connect-src[^;]*http:\/\/127\.0\.0\.1:8000[^;]*http:\/\/localhost:8000/,
  "production CSP must align localhost/127.0.0.1 API aliases with API_BASE rewriting",
);
assert.match(
  headers.get("content-security-policy") || "",
  /connect-src[^;]*ws:\/\/127\.0\.0\.1:8000[^;]*ws:\/\/localhost:8000/,
  "production CSP must align websocket localhost/127.0.0.1 aliases",
);
assert.equal(headers.get("x-content-type-options"), "nosniff");
assert.equal(headers.get("x-frame-options"), "DENY");
assert.equal(headers.get("referrer-policy"), "strict-origin-when-cross-origin");
assert.match(headers.get("permissions-policy") || "", /camera=\(\)/);
assert.match(headers.get("strict-transport-security") || "", /max-age=31536000/);
assert.match(headers.get("strict-transport-security") || "", /includeSubDomains/);

console.log("security headers test passed");
