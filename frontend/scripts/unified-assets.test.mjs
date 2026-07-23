import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  isStoredUserAsset,
  normalizeUnifiedAssetPage,
  unifiedAssetKey,
} from "../lib/unifiedAssets.js";

const page = normalizeUnifiedAssetPage({
  items: [
    { asset_ref: "g.12", origin: "generated", type: "image", preview_url: "/media/p.png", unlocked: true },
    { asset_ref: "u.dXBsb2FkL2EucG5n", origin: "uploaded", type: "image", url: "/api/uploads/upload/a.png" },
    { asset_ref: "u.dXBsb2FkL2ZldGNoZWQucG5n", origin: "fetched", type: "image", url: "/api/uploads/upload/fetched.png" },
  ],
  total: 9,
  stats: { generated: 4, uploaded: 5 },
  next_cursor: "cursor-v1",
});

assert.equal(page.items[0].id, 12);
assert.equal(page.items[0].unlocked, true);
assert.equal(page.items[1].unlocked, true);
assert.equal(page.items[2].origin, "fetched");
assert.equal(page.items[2].unlocked, true);
assert.equal(isStoredUserAsset(page.items[2]), true);
assert.equal(page.nextCursor, "cursor-v1");
assert.deepEqual(page.items.map(unifiedAssetKey), [
  "g.12",
  "u.dXBsb2FkL2EucG5n",
  "u.dXBsb2FkL2ZldGNoZWQucG5n",
]);

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const profileSource = readFileSync(join(root, "app/profile/page.jsx"), "utf8");
const pickerSource = readFileSync(join(root, "components/AssetPickerDialog.jsx"), "utf8");
assert.match(profileSource, /selectedRefs/, "asset selection must use opaque asset refs");
assert.match(profileSource, /batchDeleteMeAssets\(refs\)/, "unified deletion must use asset refs");
assert.match(profileSource, /download_url/, "asset downloads must use the owner-gated unified endpoint");
assert.doesNotMatch(profileSource, /offset: reset \? 0 : current\.length/, "cursor pagination must not also apply a cumulative offset");
assert.match(profileSource, /const hasMore = Boolean\(nextCursor\)/, "profile pagination must stop when the server cursor is exhausted");
assert.match(profileSource, /assetMeta\(asset\)/, "asset cards must expose available media metadata");
assert.match(profileSource, /retainedDelta/, "retention metadata changes must update the visible summary immediately");
assert.match(profileSource, /asset-library-search/, "the asset library must expose text search");
assert.match(profileSource, /asset-library-tag/, "the asset library must expose tag filtering");
assert.match(profileSource, /asset-library-folder/, "the asset library must expose folder filtering");
assert.match(profileSource, /\u7d20\u6750\u6293\u53d6/, "the asset library must expose fetched materials separately");
assert.match(profileSource, /moveSelectedAssets/, "selected assets must be movable between folders");
assert.match(profileSource, /findMeSimilarAssets/, "the asset library must expose duplicate and similarity analysis");
assert.match(profileSource, /similarity\.result\.status/, "degraded similarity results must remain visible");
assert.match(pickerSource, /asset-picker-search/, "the shared asset picker must expose text search");
assert.match(pickerSource, /asset-picker-tag/, "the shared asset picker must expose tag filtering");
assert.match(pickerSource, /asset-picker-folder/, "the shared asset picker must expose folder filtering");
assert.match(pickerSource, /\u7d20\u6750\u6293\u53d6/, "the shared asset picker must expose fetched materials separately");
assert.match(pickerSource, /cursor: nextCursor/, "asset picker pagination must continue with the server cursor");
assert.doesNotMatch(pickerSource, /offset: items\.length/, "asset picker cursor pagination must not skip loaded rows");
assert.match(
  pickerSource,
  /if \(loading \|\| !nextCursor\) return;/,
  "asset picker must stop loading when the server has no next cursor",
);
assert.match(
  pickerSource,
  /\{nextCursor && \(/,
  "asset picker must render load-more only while a server cursor exists",
);
assert.match(pickerSource, /excludedRefs/, "asset picker must accept opaque refs to exclude");
assert.match(pickerSource, /excludedUrls/, "asset picker must exclude an equivalent selected URL");
assert.match(
  pickerSource,
  /\u5df2\u6392\u9664\u5f53\u524d\u4ea7\u54c1\u4e3b\u9898\u56fe/,
  "product detail selection must explain why the theme image is unavailable",
);

const studioSource = readFileSync(join(root, "app/page.jsx"), "utf8");
assert.match(
  studioSource,
  /excludedRefs=\{assetPicker\?\.role === "product_detail"/,
  "Studio must exclude the current theme asset ref from detail selection",
);
assert.match(
  studioSource,
  /excludedUrls=\{assetPicker\?\.role === "product_detail"/,
  "Studio must also exclude an equivalent theme URL from detail selection",
);
assert.match(
  studioSource,
  /productDetailUploadInputRef\.current\?\.click\(\)/,
  "the detail picker must offer a direct upload continuation",
);

console.log("unified asset tests passed");
