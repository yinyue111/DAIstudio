import { expect, test } from "@playwright/test";
import { installReverseBatchApiMock } from "./fixtures/reverse-batch-api";

test.beforeEach(async ({ context, baseURL }) => {
  if (!baseURL) throw new Error("Playwright baseURL is required");
  await context.addCookies([{
    name: "ai_studio_token",
    value: "isolated-e2e-session",
    url: baseURL,
    httpOnly: true,
    sameSite: "Lax",
  }]);
});

test("多素材批量反推静默校验计费、逐项完成并批量保存配方", async ({ page }) => {
  const mock = await installReverseBatchApiMock(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "一句话，生成你的画面" })).toBeVisible();

  await page.getByRole("button", { name: "从资产选择" }).click();
  const picker = page.getByRole("dialog", { name: "选择批量反推素材" });
  await expect(picker).toBeVisible();
  await picker.getByRole("button").filter({ hasText: "batch-a.png" }).click();
  await picker.getByRole("button").filter({ hasText: "batch-b.png" }).click();
  await expect(picker.getByText("已选择 2/20", { exact: true })).toBeVisible();
  await picker.getByRole("button", { name: "确认选择 2", exact: true }).click();

  await expect(page.getByText("已选 2/20", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "开始批量反推", exact: true }).click();
  await expect(page.getByText("服务端权威报价", { exact: true })).toHaveCount(0);

  await expect(page.getByText("已创建 2 项批量反推，结果会逐项返回。")).toBeVisible();
  await expect(page.getByText("已完成 · 2/2 完成 · 4 积分", { exact: true })).toBeVisible({ timeout: 8_000 });
  await expect(page.getByText("E2E 批量生成稿 1", { exact: true })).toBeVisible();
  await expect(page.getByText("E2E 批量生成稿 2", { exact: true })).toBeVisible();

  expect(mock.batchQuoteBodies).toHaveLength(1);
  expect(mock.batchQuoteBodies[0].kind).toBe("reverse_batch");
  expect(mock.batchCreateBodies).toHaveLength(1);
  expect(mock.batchCreateBodies[0].quote_id).toBe(5701);
  expect(mock.batchCreateBodies[0].items).toHaveLength(2);
  expect(mock.batchCreateBodies[0].items.map(
    (item: Record<string, any>) => item.sources?.[0]?.asset_id,
  )).toEqual([8101, 8102]);
  expect(mock.batchDetailReads).toBeGreaterThan(0);

  await page.getByRole("button", { name: "结果对比", exact: true }).click();
  const comparison = page.getByLabel("批量反推结果对比");
  await expect(comparison.getByText("E2E 批量生成稿 1", { exact: true })).toBeVisible();
  await expect(comparison.getByText("E2E 批量生成稿 2", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "批量保存配方", exact: true }).click();
  await expect(page.getByText("批量保存完成：成功 2，失败 0。")).toBeVisible();
  expect(mock.recipeBodies).toHaveLength(2);
  expect(mock.recipeBodies.map((body) => body.source_operation_id)).toEqual([7601, 7602]);
});
