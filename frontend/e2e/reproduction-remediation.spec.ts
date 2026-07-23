import { expect, test } from "@playwright/test";

import { installReproductionRemediationApiMock } from "./fixtures/reproduction-remediation-api";

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

async function openCompletedGeneration(page) {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "一句话，生成你的画面" })).toBeVisible();
  await expect(page.getByRole("button", { name: "评估复刻度" })).toBeVisible({ timeout: 10_000 });
}

async function completeAssessment(page) {
  await page.getByRole("button", { name: "评估复刻度" }).click();
  const panel = page.getByRole("region", { name: "复刻度评估" });
  await panel.getByRole("button", { name: "开始评估" }).click();
  await expect(panel.getByText("已完成", { exact: true })).toBeVisible({ timeout: 8_000 });
  await expect(panel.getByText("当前评估还没有纠偏执行记录。", { exact: true })).toBeVisible();
  return panel;
}

test("图片差异区域局部重绘后自动复评", async ({ page }) => {
  const mock = await installReproductionRemediationApiMock(page, "image");
  await openCompletedGeneration(page);
  const panel = await completeAssessment(page);

  await expect(panel.getByText("主体一致性", { exact: true })).toBeVisible();
  await expect(panel.getByRole("button", { name: "定位问题：商品标签形状偏差" })).toBeVisible();
  await panel.getByLabel("修正提示词补丁").fill("保持商品其他区域不变");
  await panel.getByRole("button", { name: "创建局部重绘计划" }).click();

  await expect(panel.getByText("局部重绘计划 #7400", { exact: true })).toBeVisible();
  expect(mock.remediationCreateBodies).toHaveLength(1);
  expect(mock.remediationCreateBodies[0]).toMatchObject({
    parent_revision_id: 6104,
    selected_finding_ids: [9001],
    mode: "image_inpaint",
    model_config_id: 21,
    auto_reassess: true,
  });

  await panel.getByRole("button", { name: "执行计划", exact: true }).click();
  await expect(page.getByText("服务端权威报价", { exact: true })).toHaveCount(0);

  await expect(panel.getByText(/\u6700\u7ec8\u7ed3\u679c g\.7699 \u5df2\u751f\u6210/)).toBeVisible({ timeout: 8_000 });
  await expect(panel.getByText(/\u81ea\u52a8\u590d\u8bc4 #7801 \u5df2\u521b\u5efa/)).toBeVisible();
  expect(mock.generationQuoteBodies).toHaveLength(1);
  expect(mock.generationBodies).toHaveLength(1);
  expect(mock.generationBodies[0]).toMatchObject({
    quote_id: 7902,
    reproduction_remediation_id: 7400,
    reproduction_plan_item_id: "image-region-item",
  });
});

test("视频差异镜头逐项重生成、合成后自动复评", async ({ page }) => {
  const mock = await installReproductionRemediationApiMock(page, "video");
  await openCompletedGeneration(page);
  const panel = await completeAssessment(page);

  await expect(panel.getByText("动作一致性", { exact: true })).toBeVisible();
  await expect(panel.getByRole("button", { name: "选择时间线问题：镜头 A 主体动作偏差" })).toBeVisible();
  await expect(panel.getByRole("button", { name: "选择时间线问题：镜头 B 推近速度不一致" })).toBeVisible();
  await panel.getByRole("button", { name: "创建逐镜重生成计划" }).click();

  await expect(panel.getByText("逐镜重生成计划 #7400", { exact: true })).toBeVisible();
  expect(mock.remediationCreateBodies).toHaveLength(1);
  expect(mock.remediationCreateBodies[0]).toMatchObject({
    parent_revision_id: 6104,
    selected_finding_ids: [9001, 9002],
    selected_shot_ids: ["shot-a", "shot-b"],
    mode: "video_shot_regenerate",
    model_config_id: 31,
    auto_reassess: true,
  });

  await panel.getByRole("button", { name: "执行计划 (2)", exact: true }).click();
  await expect(page.getByText("服务端权威报价", { exact: true })).toHaveCount(0);

  await expect(panel.getByText("已重生成并合成成片", { exact: true })).toBeVisible({ timeout: 8_000 });
  await expect(panel.getByText(/\u6700\u7ec8\u7ed3\u679c g\.7699 \u5df2\u751f\u6210/)).toBeVisible();
  await expect(panel.getByText(/\u81ea\u52a8\u590d\u8bc4 #7801 \u5df2\u521b\u5efa/)).toBeVisible();
  expect(mock.generationQuoteBodies).toHaveLength(2);
  expect(mock.generationBodies.map((body) => body.reproduction_plan_item_id)).toEqual([
    "shot-a-item",
    "shot-b-item",
  ]);
  expect(mock.generationBodies.map((body) => body.shot_context?.shot_id)).toEqual([
    "shot-a",
    "shot-b",
  ]);
});
