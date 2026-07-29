import { expect, test } from "@playwright/test";
import {
  installReverseApiMock,
  sharedRecipeFixture,
  type SharedRecipeStatus,
} from "./fixtures/reverse-api";

const SHARE_SLUG = "E2ESharedRecipeSlug_1234";
const SHARE_PATH = `/recipes/shared/${SHARE_SLUG}`;

async function authenticate(context, baseURL: string | undefined) {
  if (!baseURL) throw new Error("Playwright baseURL is required");
  await context.addCookies([{
    name: "ai_studio_token",
    value: "recipe-sharing-e2e-session",
    url: baseURL,
    httpOnly: true,
    sameSite: "Lax",
  }]);
}

test("匿名用户可查看脱敏分享，并在使用时保留登录回跳地址", async ({ page }) => {
  const shared = sharedRecipeFixture(SHARE_SLUG);
  const mock = await installReverseApiMock(page, {
    authenticated: false,
    sharedRecipe: shared,
  });

  await page.goto(SHARE_PATH);

  await expect(page.getByRole("heading", { name: "E2E 视频广告完整配方" })).toBeVisible();
  await expect(page.getByText("精确生成稿：银色香水瓶居中，镜头缓慢推进", { exact: true })).toBeVisible();
  await expect(page.getByText("私有源素材已脱敏，使用时需要重新选择本人的参考素材。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "登录后使用" })).toBeVisible();
  await expect(page.getByRole("button", { name: "登录后派生" })).toBeVisible();
  expect(await page.locator("body").innerText()).not.toContain("/api/uploads/");
  expect(mock.sharedRecipeReads).toBe(1);

  await page.getByRole("button", { name: "登录后使用" }).click();
  await expect.poll(() => new URL(page.url()).pathname, { timeout: 20_000 }).toBe("/login");
  expect(new URL(page.url()).searchParams.get("next")).toBe(SHARE_PATH);
});

test("登录用户使用分享后精确写入草稿并在 Studio 恢复完整配方", async ({ page, context, baseURL }) => {
  await authenticate(context, baseURL);
  await page.addInitScript(() => {
    const target = window as typeof window & { __recipeDraftWrites?: Array<{ key: string; value: string }> };
    const original = Storage.prototype.setItem;
    const captureKey = "__e2eRecipeDraftWrites";
    const captured = sessionStorage.getItem(captureKey);
    target.__recipeDraftWrites = captured ? JSON.parse(captured) : [];
    Storage.prototype.setItem = function setItem(key, value) {
      if (String(key).startsWith("studio:draftPrompt")) {
        target.__recipeDraftWrites?.push({ key: String(key), value: String(value) });
        original.call(sessionStorage, captureKey, JSON.stringify(target.__recipeDraftWrites));
      }
      return original.call(this, key, value);
    };
  });
  const shared = sharedRecipeFixture(SHARE_SLUG);
  const mock = await installReverseApiMock(page, { sharedRecipe: shared });

  await page.goto(SHARE_PATH);
  await page.getByRole("button", { name: "在 Studio 使用" }).click();

  await expect(page).toHaveURL(/\/$/, { timeout: 20_000 });
  const transfer = await page.evaluate(() => {
    const target = window as typeof window & { __recipeDraftWrites?: Array<{ key: string; value: string }> };
    const captured = sessionStorage.getItem("__e2eRecipeDraftWrites");
    const writes = captured
      ? JSON.parse(captured) as Array<{ key: string; value: string }>
      : target.__recipeDraftWrites || [];
    const write = writes.at(-1);
    return write ? { key: write.key, draft: JSON.parse(write.value) } : null;
  });
  expect(transfer?.key).toBe("studio:draftPrompt:user:9001");
  expect(transfer?.draft).toMatchObject({
    ownerUserId: "9001",
    prompt: "精确生成稿：银色香水瓶居中，镜头缓慢推进",
    negative: "不要品牌文字漂移",
    structured: { "主体": "银色香水瓶", "运镜": "slow dolly in" },
    category: "video",
    creationMode: "video_edit",
    creationRecipeId: 42,
    creationRecipeVersion: 3,
    creationRecipeShareSlug: SHARE_SLUG,
    creationRecipeSource: "share",
    generation: {
      ratio: "9:16",
      duration: 8,
      resolution: "1080p",
      count: 2,
      seed: 987654,
      model_selections: { image: 21, video: 31, vision: 11, prompt: 41 },
    },
    reverse_snapshot_v3: {
      version: 3,
      target: "video",
      analysis_focus: "storyboard",
      analysis_precision: "fine",
      source_ranges: [{ start_seconds: 2, end_seconds: 6 }],
      custom_keyframes: [2, 4, 6],
      include_audio: true,
      creation_recipe_id: 42,
      creation_recipe_version: 3,
      creation_recipe_share_slug: SHARE_SLUG,
      creation_recipe_source: "share",
    },
  });
  expect(transfer?.draft.reverse_snapshot_v3).not.toHaveProperty("public_asset_access");
  expect(JSON.stringify(transfer?.draft)).not.toContain("/api/uploads/");

  await expect(page.getByText("已恢复反推文字、结构和分析证据；过期素材已跳过，请重新上传。", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: /^图生视频：/ }))
    .toHaveValue("精确生成稿：银色香水瓶居中，镜头缓慢推进");
  await expect(page.getByRole("button", { name: /图生视频/ })).toHaveClass(/bg-brand/);
  await expect(page.locator("label").filter({ hasText: /^主体$/ }).locator("xpath=following-sibling::input"))
    .toHaveValue("银色香水瓶");
  await expect(page.locator('input[type="number"][title^="最长"]')).toHaveValue("8");
  await expect(page.locator("button.chip").filter({ hasText: /^1080p$/ })).toHaveClass(/chip-active/);
  const storyboard = page.getByRole("list", { name: "反推分镜" });
  await expect(storyboard).toBeVisible();
  await expect(storyboard.getByRole("textbox", { name: "画面" })).toHaveValue("香水瓶居中");
  await expect(storyboard.getByRole("textbox", { name: "动作" })).toHaveValue("瓶身缓慢旋转");
  await expect(storyboard.getByRole("textbox", { name: "镜头" })).toHaveValue("slow dolly in");
  await expect(page.getByRole("region", { name: "合成与导出" })).toBeVisible();

  expect(mock.usageBodies).toHaveLength(1);
  expect(mock.usageBodies[0]).toMatchObject({
    event_type: "apply",
    version: 3,
    share_slug: SHARE_SLUG,
    context: { entry: "shared_recipe" },
  });
  expect(mock.usageBodies[0].client_event_id).toMatch(/^recipe-share-apply-/);
});

test("登录用户可将分享派生为自己的私有配方", async ({ page, context, baseURL }) => {
  await authenticate(context, baseURL);
  const shared = sharedRecipeFixture(SHARE_SLUG);
  const mock = await installReverseApiMock(page, { sharedRecipe: shared });

  await page.goto(SHARE_PATH);
  await page.getByRole("button", { name: "派生到我的配方" }).click();

  await expect(page.getByRole("button", { name: "已派生到我的配方" })).toBeVisible();
  await expect(page.getByRole("link", { name: /查看我的配方/ })).toHaveAttribute("href", "/prompts");
  expect(mock.cloneBodies).toEqual([{ version: 3, share_slug: SHARE_SLUG }]);
});

for (const status of ["revoked", "expired"] satisfies SharedRecipeStatus[]) {
  test(`${status === "revoked" ? "已撤销" : "已过期"}分享显示不可用错误页`, async ({ page }) => {
    const mock = await installReverseApiMock(page, {
      authenticated: false,
      sharedRecipe: sharedRecipeFixture(SHARE_SLUG),
      sharedRecipeStatus: status,
    });

    await page.goto(SHARE_PATH);

    await expect(page.getByRole("heading", { name: "分享链接不可用" })).toBeVisible();
    await expect(page.getByRole("alert").filter({ hasText: "分享链接不可用" }))
      .toContainText("配方分享不存在、已撤销或已过期。");
    await expect(page.getByRole("button", { name: "重新加载" })).toBeVisible();
    expect(mock.sharedRecipeReads).toBe(1);
  });
}
