import assert from "node:assert/strict";

process.env.NEXT_PUBLIC_API_BASE = "http://127.0.0.1:8000";
process.env.NEXT_PUBLIC_MEDIA_SRC = "";
process.env.NODE_ENV = "development";

globalThis.window = {
  location: new URL("http://127.0.0.1:3002/"),
};

const {
  assetDisplaySrc,
  assetPreviewSrc,
  safeAssetMediaSrc,
  shouldRenderVideo,
} = await import("../components/AssetMedia.jsx");

assert.equal(
  safeAssetMediaSrc("/media/preview/example.png"),
  "/media/preview/example.png",
  "ordinary platform-relative media paths should remain available",
);
assert.equal(
  safeAssetMediaSrc("blob:http://127.0.0.1:3002/local-preview"),
  "blob:http://127.0.0.1:3002/local-preview",
  "local object URL previews should remain available",
);

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
assert.equal(
  safeAssetMediaSrc("\\\\evil.example/media/preview/example.png"),
  "",
  "backslash authority URLs must not be treated as same-origin media",
);
assert.equal(
  safeAssetMediaSrc("/\\evil.example/media/preview/example.png"),
  "",
  "slash-backslash authority URLs must not bypass the media allowlist",
);
assert.equal(safeAssetMediaSrc("javascript:alert(1)"), "");

const uploadedVideo = {
  origin: "uploaded",
  type: "video",
  url: "/api/uploads/upload_video/product.mp4",
  preview_url: "/api/uploads/upload_video_preview/product.jpg",
  unlocked: true,
};
assert.equal(
  assetDisplaySrc(uploadedVideo),
  uploadedVideo.preview_url,
  "uploaded video cards should render the poster instead of loading MP4 bytes as an image",
);
assert.equal(
  shouldRenderVideo(uploadedVideo),
  false,
  "uploaded video cards with a poster should render the poster frame",
);
assert.equal(
  assetDisplaySrc(uploadedVideo, { interactive: true }),
  uploadedVideo.url,
  "interactive uploaded-video previews should use the playable MP4 URL",
);
assert.equal(
  shouldRenderVideo(uploadedVideo, { interactive: true }),
  true,
  "interactive uploaded-video previews should render a video player",
);

const generatedVideo = {
  origin: "generated",
  type: "video",
  preview_url: "/media/video_preview/generated.mp4",
  unlocked: true,
};
assert.equal(assetDisplaySrc(generatedVideo), generatedVideo.preview_url);
assert.equal(shouldRenderVideo(generatedVideo), true, "generated video card behavior must stay unchanged");

console.log("asset media source normalization test passed");
