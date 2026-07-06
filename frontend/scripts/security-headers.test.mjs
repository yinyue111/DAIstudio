import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

process.env.NODE_ENV = "production";
process.env.NEXT_PUBLIC_API_BASE = "http://127.0.0.1:8000";
const configModule = await import(join(dirname(fileURLToPath(import.meta.url)), "../next.config.js"));
const config = configModule.default;
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
assert.doesNotMatch(
  headers.get("content-security-policy") || "",
  /connect-src[^;]*http:\/\/(?:127\.0\.0\.1|localhost):8000/,
  "production CSP must not allow loopback API origins unless LAN aliasing is explicitly enabled",
);
assert.doesNotMatch(
  headers.get("content-security-policy") || "",
  /connect-src[^;]*http:\/\/\*:8000/,
  "production CSP must not ship LAN wildcard API rewrites by default",
);
assert.doesNotMatch(
  headers.get("content-security-policy") || "",
  /connect-src[^;]*ws:\/\/(?:127\.0\.0\.1|localhost):8000/,
  "production CSP must not allow loopback websocket origins unless LAN aliasing is explicitly enabled",
);
assert.doesNotMatch(
  headers.get("content-security-policy") || "",
  /connect-src[^;]*ws:\/\/\*:8000/,
  "production CSP must not ship LAN wildcard websocket rewrites by default",
);
assert.equal(headers.get("x-content-type-options"), "nosniff");
assert.equal(headers.get("x-frame-options"), "DENY");
assert.equal(headers.get("referrer-policy"), "strict-origin-when-cross-origin");
assert.match(headers.get("permissions-policy") || "", /camera=\(\)/);
assert.match(headers.get("strict-transport-security") || "", /max-age=31536000/);
assert.match(headers.get("strict-transport-security") || "", /includeSubDomains/);

console.log("security headers test passed");
