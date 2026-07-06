import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { NextRequest } from "next/server.js";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const { proxy } = await import(join(root, "proxy.js"));

function request(path, cookieValue = "") {
  return new NextRequest(`http://127.0.0.1:3002${path}`, {
    headers: cookieValue ? { cookie: `ai_studio_token=${cookieValue}` } : {},
  });
}

async function middlewareResult(path, cookieValue = "") {
  const response = await proxy(request(path, cookieValue));
  const location = response.headers.get("location") || "";
  const locationUrl = location ? new URL(location) : null;
  return {
    status: response.status,
    location,
    path: locationUrl ? `${locationUrl.pathname}${locationUrl.search}` : "",
  };
}

{
  const result = await middlewareResult("/");
  assert.equal(result.status, 307, "unauthenticated home should redirect");
  assert.equal(result.path, "/login", "unauthenticated home should default to /login");
}
{
  const result = await middlewareResult("/profile?filter=image#ignored");
  assert.equal(result.status, 307, "unauthenticated protected page should redirect");
  assert.equal(
    result.path,
    "/login?next=%2Fprofile%3Ffilter%3Dimage",
    "unauthenticated protected pages should preserve a safe next path",
  );
}
{
  const result = await middlewareResult("/profile?phone=13800000000&password=secret&filter=image");
  assert.equal(result.status, 307, "unauthenticated protected page should redirect");
  assert.equal(
    result.path,
    "/login?next=%2Fprofile%3Ffilter%3Dimage",
    "middleware next path must strip credential-like query params",
  );
}
assert.equal(
  (await middlewareResult("/login")).status,
  200,
  "login page must remain public",
);
assert.equal(
  (await middlewareResult("/icon.svg")).status,
  200,
  "public static files must remain public so login can render",
);
assert.equal(
  (await middlewareResult("/_next/webpack-hmr")).status,
  200,
  "Next dev resources must remain public so client JS can hydrate",
);
assert.equal(
  (await middlewareResult("/api/health")).status,
  200,
  "API paths must not be redirected by frontend middleware",
);

const originalFetch = globalThis.fetch;
const originalInternalApiBase = process.env.API_INTERNAL_BASE;
const calls = [];
process.env.API_INTERNAL_BASE = "http://api:8000";
globalThis.fetch = async (url, options) => {
  calls.push({ url: String(url), cookie: options?.headers?.cookie || "" });
  return new Response("{}", { status: 200 });
};
assert.equal(
  (await middlewareResult("/", "valid-cookie-shape")).status,
  200,
  "authenticated home should render the studio after /api/me validation",
);
assert.equal(
  (await middlewareResult("/admin", "valid-cookie-shape")).status,
  200,
  "authenticated protected pages should be allowed through after /api/me validation",
);
assert.equal(calls.length, 2, "protected authenticated requests should validate against /api/me");
assert.ok(calls.every((call) => call.url.endsWith("/api/me")), "session validation must call /api/me");
assert.ok(
  calls.every((call) => call.url.startsWith("http://api:8000/api/me")),
  "server-side session validation should use API_INTERNAL_BASE when configured",
);
assert.ok(
  calls.every((call) => call.cookie.includes("ai_studio_token=valid-cookie-shape")),
  "session validation must forward the HttpOnly auth cookie",
);

globalThis.fetch = async () => new Response("{}", { status: 401 });
{
  const result = await middlewareResult("/history", "expired-cookie-shape");
  assert.equal(result.status, 307, "invalid cookies must not render protected pages");
  assert.equal(result.path, "/login?next=%2Fhistory", "invalid cookies should redirect to login with next path");
}

globalThis.fetch = originalFetch;
if (originalInternalApiBase === undefined) delete process.env.API_INTERNAL_BASE;
else process.env.API_INTERNAL_BASE = originalInternalApiBase;

assert.equal(
  (await middlewareResult("/prompt-library/thumbs/example.jpg")).status,
  200,
  "prompt-library static preview assets should remain public",
);

console.log("auth middleware test passed");
