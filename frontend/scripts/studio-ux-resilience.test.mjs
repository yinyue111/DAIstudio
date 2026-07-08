import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageSource = readFileSync(join(root, "app/page.jsx"), "utf8");
const apiSource = readFileSync(join(root, "lib/api.js"), "utf8");
const errorSource = readFileSync(join(root, "lib/errorHandling.js"), "utf8");
const mediaUploadSource = readFileSync(join(root, "hooks/useMediaUpload.js"), "utf8");
const referenceParsingSource = readFileSync(join(root, "hooks/useReferenceParsing.js"), "utf8");
const taskTrackingSource = readFileSync(join(root, "hooks/useTaskTracking.js"), "utf8");
const studioResultsSource = readFileSync(join(root, "app/studio/StudioResults.jsx"), "utf8");
const promptsPageSource = readFileSync(join(root, "app/prompts/page.jsx"), "utf8");
const toastSource = readFileSync(join(root, "components/ToastProvider.jsx"), "utf8");

assert.doesNotMatch(
  pageSource,
  /catch \(e\) \{\s*if \(seq === loadWorksSeqRef\.current\) setWorks\(\[\]\);/,
  "workspace gallery load failure must preserve existing works instead of clearing the wall",
);
assert.match(
  pageSource,
  /setWorksError\(/,
  "workspace gallery load failure should expose a visible retryable error",
);
assert.match(
  pageSource,
  /saveStudioSessionDraft\(/,
  "studio should save workspace state before auth redirects can discard edits",
);
assert.match(
  apiSource,
  /setUnauthorizedHandler/,
  "API client should support a shared 401 hook for workspace draft backup",
);
assert.match(
  errorSource,
  /classifyGenerationError/,
  "user-facing generation failures should be classified into actionable categories",
);
assert.match(
  taskTrackingSource,
  /MAX_TASK_WS_RECONNECT_ATTEMPTS\s*=\s*3/,
  "active task websocket should retry three times before falling back to polling",
);
assert.match(
  taskTrackingSource,
  /ACTIVE_VIDEO_POLL_INTERVAL_MS\s*=\s*4000/,
  "video polling should be relaxed because renders take minutes",
);
assert.match(
  taskTrackingSource,
  /estimateVideoRemainingText/,
  "video tasks should expose a rough remaining-time hint while running",
);
assert.match(
  taskTrackingSource,
  /trackingRunRef/,
  "active task tracking should invalidate stale websocket and polling closures when restarted",
);
assert.match(
  taskTrackingSource,
  /startPolling\(id,\s*runId\)/,
  "websocket fallback should pass the current tracking generation into polling",
);
assert.match(
  mediaUploadSource,
  /prefetchProductProfile/,
  "product uploads should prefetch subject profiles before submit",
);
assert.match(
  referenceParsingSource,
  /lastReversePromptRef/,
  "reverse prompt results should remain available for saving to prompt history",
);
assert.match(
  pageSource,
  /saveReversePromptToLibrary/,
  "studio should let users save reverse prompt output to their prompt library",
);
assert.match(
  studioResultsSource,
  /内容审核中|管理员已收到/,
  "needs_review tasks should tell the user that the result is under review",
);
assert.doesNotMatch(
  pageSource,
  /variationSourceUrl\(/,
  "variation draft recovery must not call an undefined variationSourceUrl helper",
);
assert.match(
  pageSource,
  /assetVariationSourceUrl\(asset,\s*\{\s*respectUnlock:\s*true\s*\}\)/,
  "variation draft recovery should reuse the canonical asset source helper",
);
assert.match(
  pageSource,
  /if \(applyVariationDraft\(JSON\.parse\(variationDraft\)\)\) \{\s*window\.localStorage\.removeItem\(STUDIO_VARIATION_DRAFT_KEY\);/s,
  "variation draft should only be removed after successful recovery",
);
assert.match(
  errorSource,
  /error\?\.error_message \|\| error\?\.error/,
  "generation error classifier should preserve task error_message/error details",
);
assert.match(
  promptsPageSource,
  /JSON\.stringify\(\{[\s\S]*category:\s*item\.category/,
  "prompt library handoff should preserve prompt category for video prompts",
);
assert.match(
  promptsPageSource,
  /savedAt:\s*Date\.now\(\)/,
  "prompt library handoff should timestamp drafts so stale cloud drafts cannot overwrite them",
);
assert.match(
  pageSource,
  /function parsePromptDraft/,
  "studio should read structured prompt drafts while remaining compatible with legacy string drafts",
);
assert.match(
  pageSource,
  /CREATION_MODES\.some\(\(item\) => item\.key === draft\.creationMode\)/,
  "structured prompt drafts should validate mode keys against creation-mode objects",
);
assert.match(
  pageSource,
  /restoredLocalDraftAtRef\.current = Number\(parsedDraft\.savedAt \|\| Date\.now\(\)\)/,
  "prompt library handoff should block older cloud workspace drafts from overriding the selected prompt",
);
assert.match(
  toastSource,
  /bottom-\[calc\(5\.75rem\+env\(safe-area-inset-bottom\)\)\]/,
  "mobile toast should sit above the fixed submit bar",
);

console.log("studio UX resilience test passed");
