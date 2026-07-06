import assert from "node:assert/strict";

process.env.NEXT_PUBLIC_API_BASE = "http://127.0.0.1:8000";
process.env.NEXT_PUBLIC_MEDIA_SRC = "";
process.env.NODE_ENV = "development";

globalThis.window = {
  location: new URL("http://127.0.0.1:3002/"),
};

const { assetPreviewSrc, safeAssetMediaSrc } = await import("../components/AssetMedia.jsx");

assert.equal(
  safeAssetMediaSrc("http://localhost:8000/media/preview/example.png"),
  "http://127.0.0.1:8000/media/preview/example.png",
  "loopback platform media URLs should follow the resolved API origin",
);
assert.equal(
  assetPreviewSrc({ preview_url: "http://localhost:8000/media/preview/example.png" }),
  "http://127.0.0.1:8000/media/preview/example.png",
  "result cards should keep previews when backend PUBLIC_BASE_URL uses localhost",
);
assert.equal(
  safeAssetMediaSrc("http://localhost:9000/media/preview/example.png"),
  "",
  "loopback aliases must still match the API port",
);
assert.equal(
  safeAssetMediaSrc("https://evil.example/media/preview/example.png"),
  "",
  "external media origins must stay blocked unless explicitly allowlisted",
);
process.env.NEXT_PUBLIC_MEDIA_SRC = "https://cdn-a.example, https://cdn-b.example";
assert.equal(
  safeAssetMediaSrc("https://cdn-b.example/media/preview/example.png"),
  "https://cdn-b.example/media/preview/example.png",
  "comma-separated media allowlist origins should match CSP parsing",
);
process.env.NEXT_PUBLIC_MEDIA_SRC = "";
assert.equal(
  safeAssetMediaSrc("//evil.example/media/preview/example.png"),
  "",
  "protocol-relative external media URLs must not be treated as platform paths",
);
assert.equal(safeAssetMediaSrc("javascript:alert(1)"), "");

console.log("asset media source normalization test passed");
