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
const { normalizeLoopbackPlatformMediaUrl } = await import("../lib/platformMedia.js");

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
  normalizeLoopbackPlatformMediaUrl(
    "http://localhost:8000/api/uploads/upload_video_preview/example.jpg",
    {
      apiBase: "http://127.0.0.1:8000",
      pageOrigin: "http://127.0.0.1:3002",
      allowedPrefixes: ["/api/uploads/"],
    },
  ),
  "http://127.0.0.1:8000/api/uploads/upload_video_preview/example.jpg",
  "authenticated upload previews should normalize equivalent loopback API hosts",
);
assert.equal(
  normalizeLoopbackPlatformMediaUrl(
    "http://127.0.0.1:8000/api/uploads/upload_video_preview/example.jpg",
    {
      apiBase: "http://localhost:8000",
      pageOrigin: "http://localhost:3002",
      allowedPrefixes: ["/api/uploads/"],
    },
  ),
  "http://localhost:8000/api/uploads/upload_video_preview/example.jpg",
  "authenticated upload previews should normalize loopback aliases in both directions",
);
assert.equal(
  normalizeLoopbackPlatformMediaUrl(
    "http://localhost:8000/media/preview/example.png",
    {
      apiBase: "http://127.0.0.1:8000",
      pageOrigin: "http://127.0.0.1:3002",
      allowedPrefixes: ["/api/uploads/"],
    },
  ),
  "",
  "authenticated upload normalization must not broaden to unrelated platform paths",
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

const fetchedImage = {
  origin: "fetched",
  type: "image",
  url: "/api/uploads/upload/fetched-product.png",
  unlocked: true,
};
assert.equal(
  assetDisplaySrc(fetchedImage),
  fetchedImage.url,
  "fetched materials should use the same owner-controlled media path as manual uploads",
);

const studioUploadedVideo = {
  type: "video",
  display_url: "blob:http://127.0.0.1:3010/local-video",
  display_thumb: "/api/uploads/upload_video_preview/local-video.jpg",
  url: "/api/uploads/upload_video/local-video.mp4",
};
assert.equal(
  assetDisplaySrc(studioUploadedVideo),
  studioUploadedVideo.display_thumb,
  "non-interactive Studio video sources must prefer their poster over the MP4 blob",
);
assert.equal(
  shouldRenderVideo(studioUploadedVideo),
  false,
  "a Studio upload with a poster must render as an image thumbnail",
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
