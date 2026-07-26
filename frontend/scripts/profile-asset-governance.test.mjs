import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(root, "app/profile/page.jsx"), "utf8");
const apiSource = readFileSync(join(root, "lib/api.js"), "utf8");

// ---- 举报入口：素材卡片 + 预览详情 → 弹窗 → api.reportAsset ----
assert.match(
  source,
  /api\.reportAsset\(assetId,\s*\{\s*reason:\s*reportReason,\s*note:/,
  "submitReport should call api.reportAsset with reason and note",
);
assert.match(
  apiSource,
  /reportAsset:\s*\(assetId,\s*body\)\s*=>\s*\n?\s*request\(`\/api\/assets\/\$\{assetId\}\/report`/,
  "api.reportAsset should post to the backend report endpoint",
);
// 理由枚举必须与后端 AssetReportIn 的 Literal 一致
for (const reason of ["copyright", "sensitive", "illegal", "privacy", "other"]) {
  assert.match(
    source,
    new RegExp(`\\["${reason}",\\s*"[^"]+"\\]`),
    `report reason ${reason} should be selectable and match backend schema`,
  );
}
assert.match(
  source,
  /maxLength=\{500\}/,
  "report note input should enforce the backend 500-char limit",
);
assert.match(
  source,
  /asset\.origin === "generated" && \(\s*<button[^>]*onClick=\{\(\) => openReport\(asset\)\}/,
  "asset card should expose a report button for generated assets",
);
assert.match(
  source,
  /onClick=\{\(\) => openReport\(asset\)\}[^>]*className="btn-ghost btn-sm">举报<\/button>/,
  "preview dialog should expose a report action",
);
assert.match(
  source,
  /onClick=\{submitReport\}/,
  "report dialog submit button should be wired to submitReport",
);
assert.match(
  source,
  /你已举报过该素材/,
  "duplicate reports should get an explicit notice",
);
assert.match(
  source,
  /举报已受理/,
  "new reports should get an accepted notice",
);

// ---- 批量下载：多选工具条 → api.batchDownloadAssets（downloadBlob，需处理 loading 与失败） ----
assert.match(
  source,
  /api\.batchDownloadAssets\(ids\)/,
  "batchDownloadSelected should call api.batchDownloadAssets",
);
assert.match(
  apiSource,
  /batchDownloadAssets:\s*\(assetIds\)\s*=>\s*downloadBlob\("\/api\/assets\/batch\/download"/,
  "api.batchDownloadAssets should download a zip blob from the batch endpoint",
);
assert.match(
  source,
  /onClick=\{batchDownloadSelected\}/,
  "the multi-select toolbar should bind the batch download handler",
);
assert.match(
  source,
  /disabled=\{batchDownloading \|\| !selectedDownloadableIds\.length\}/,
  "batch download button should be disabled while packing or without downloadable assets",
);
assert.match(
  source,
  /batchDownloading \? "打包中…"/,
  "batch download button should show a loading state",
);
assert.match(
  source,
  /notify\.error\(error\.message \|\| "打包下载失败，请稍后再试"\)/,
  "batch download failures should surface a toast",
);
assert.match(
  source,
  /origin === "generated" && canDownloadAsset\(asset\)/,
  "only unlocked generated assets should be sent to the batch download endpoint",
);
assert.match(
  source,
  /BATCH_DOWNLOAD_LIMIT = 100/,
  "batch download should respect the backend limit of 100 asset ids",
);

console.log("profile asset governance entry checks passed");
