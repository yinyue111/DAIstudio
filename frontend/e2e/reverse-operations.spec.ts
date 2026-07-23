import { expect, test } from "@playwright/test";
import {
  installReverseApiMock,
  reverseHistoryV2Row,
} from "./fixtures/reverse-api";

const IMAGE_FILE = {
  name: "reverse-e2e.png",
  mimeType: "image/png",
  buffer: Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
    "base64",
  ),
};
const VIDEO_FILE = {
  name: "reverse-e2e.mp4",
  mimeType: "video/mp4",
  buffer: Buffer.from("e2e-video-placeholder"),
};

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

async function openStudio(page) {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "一句话，生成你的画面" })).toBeVisible();
}

async function expectPromptOptimizerOnly(page) {
  await expect(page.getByLabel("优化模型", { exact: true })).toBeVisible();
  await expect(page.getByLabel(/优化模型消耗 单次 \d+ 积分/)).toBeVisible();
  await expect(page.getByRole("button", { name: "生成优化建议" })).toBeVisible();
  await expect(page.getByRole("button", { name: "提示词库", exact: true })).toHaveCount(0);
  for (const preset of [
    "赛博朋克城市夜景",
    "宇航服的柴犬",
    "极简北欧风咖啡馆",
    "国潮水墨山水",
    "保留品牌标识",
    "替换为干净棚拍背景",
    "同款光线和构图",
    "参考视频动作节奏",
  ]) {
    await expect(page.locator("button.chip").filter({ hasText: preset })).toHaveCount(0);
  }
}

async function expectAllStudioModesWithoutPresets(page) {
  for (const mode of [
    { label: "文生图", generationModel: "图片模型" },
    { label: "图片编辑", generationModel: "图片模型" },
    { label: "文生视频", generationModel: "视频模型" },
    { label: "图生视频", generationModel: "视频模型" },
  ]) {
    await page.getByRole("button", { name: new RegExp(mode.label) }).click();
    await expect(page.getByLabel(mode.generationModel, { exact: true })).toBeVisible();
    await expectPromptOptimizerOnly(page);
  }
}

async function uploadImageReference(page) {
  await page.locator('input[type="file"][accept*="image/jpeg"]').first().setInputFiles(IMAGE_FILE);
  await expect(page.getByRole("button", { name: /image$/ })).toBeVisible();
}

async function startImageReverse(page) {
  await uploadImageReference(page);
  await page.getByRole("button", { name: /反推提示词.*2积分/ }).click();
  await expect(page.getByText("服务端权威报价", { exact: true })).toHaveCount(0);
}

test("图片反推成功，WebSocket 失败后轮询，并拦截结构化冲突", async ({ page }) => {
  const mock = await installReverseApiMock(page, { scenario: "image_success" });
  await openStudio(page);
  await page.getByLabel("反推模型", { exact: true }).selectOption("12");
  await startImageReverse(page);

  await expect(page.getByText("反推完成，已结算 2 积分")).toBeVisible();
  const structureTab = page.getByRole("tab", { name: "结构参数" });
  await expect(structureTab).toBeVisible();
  await expect.poll(() => mock.wsTicketRequests).toBeGreaterThan(0);
  await expect.poll(() => mock.operationReads).toBeGreaterThan(0);
  expect(mock.quoteRequests).toHaveLength(1);
  expect(mock.quoteRequests[0].kind).toBe("reverse");
  expect(mock.quoteRequests[0].request.model_config_id).toBe(12);
  expect(mock.quoteRequests[0].request.workspace_snapshot_v3.version).toBe(3);
  expect(mock.createBodies).toHaveLength(1);
  expect(mock.createBodies[0].model_config_id).toBe(12);
  expect(mock.createBodies[0].quote_id).toBe(5001);
  expect(mock.createBodies[0].client_request_id).toMatch(/^studio-reverse-image-/);
  expect(mock.createBodies[0].workspace_snapshot_v3.version).toBe(3);

  await structureTab.click();
  await page.getByRole("tabpanel").getByRole("textbox", { name: "主体", exact: true })
    .fill("改后的 E2E 主体");
  await expect(page.getByText("7/7", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "替换当前" }).click();
  await expect(page.getByText("反推结果已应用，可随时撤销本次字段变更。")).toBeVisible();
  expect(mock.applyBodies).toHaveLength(1);

  await page.getByRole("tab", { name: "生成稿" }).click();
  const dimensionsToggle = page.getByRole("button", { name: /反推维度/ });
  await expect(dimensionsToggle).toBeVisible();
  const subjectField = page.locator("label").filter({ hasText: /^主体$/ }).locator("xpath=following-sibling::input");
  if (!await subjectField.isVisible()) await dimensionsToggle.click();
  const prompt = page.getByPlaceholder(/描述你想要的画面/);
  await prompt.fill("用户手工改写的提示词");
  await subjectField.fill("二次修改的 E2E 主体");

  const conflict = page.getByRole("alert").filter({ hasText: "提示词已手工修改" });
  await expect(conflict).toBeVisible();
  await expect(page.getByRole("button", { name: /立即生成/ })).toBeDisabled();
  await conflict.getByRole("button", { name: "应用结构修改" }).click();
  await expect(conflict).toBeHidden();
  await expect(prompt).toHaveValue(/二次修改的 E2E 主体/);
  await expect(page.getByRole("button", { name: /立即生成/ })).toBeEnabled();
});

test("后台模型目录动态渲染四类模型下拉并支持切换", async ({ page }) => {
  await installReverseApiMock(page, { scenario: "image_success" });
  await openStudio(page);

  const vision = page.getByLabel("反推模型", { exact: true });
  const image = page.getByLabel("图片模型", { exact: true });
  const prompt = page.getByLabel("优化模型", { exact: true });
  await expect(vision.locator("option")).toHaveCount(2);
  await expect(image.locator("option")).toHaveCount(2);
  await expect(prompt.locator("option")).toHaveCount(2);
  await image.selectOption("22");
  await prompt.selectOption("42");
  await expect(image).toHaveValue("22");
  await expect(prompt).toHaveValue("42");

  await page.getByRole("button", { name: /文生视频/ }).click();
  const video = page.getByLabel("视频模型", { exact: true });
  await expect(video.locator("option")).toHaveCount(2);
  await video.selectOption("32");
  await expect(video).toHaveValue("32");
});

test("四种创作模式仅保留提示词优化控件，不展示预制提示词", async ({ page }) => {
  await installReverseApiMock(page, { scenario: "image_success" });
  await openStudio(page);
  await expectAllStudioModesWithoutPresets(page);

  await page.setViewportSize({ width: 390, height: 844 });
  await expectAllStudioModesWithoutPresets(page);
});

test("提示词优化静默校验计费并直接生成建议", async ({ page }) => {
  const mock = await installReverseApiMock(page, { scenario: "image_success" });
  await openStudio(page);

  await page.getByPlaceholder(/描述你想要的画面/).fill("玻璃杯产品广告，柔和侧光");
  await page.getByRole("button", { name: "生成优化建议", exact: true }).click();

  await expect(page.getByRole("dialog", { name: "确认提示词优化" })).toHaveCount(0);
  await expect(page.getByText("优化建议已生成，请对比后选择接受或拒绝。")).toBeVisible();
  await expect(page.getByText("E2E 目标模型编译稿", { exact: true })).toBeVisible();
  expect(mock.quoteRequests).toHaveLength(1);
  expect(mock.quoteRequests[0].kind).toBe("prompt_optimization");
  expect(mock.promptOptimizationBodies).toHaveLength(1);
  expect(mock.promptOptimizationBodies[0].quote_id).toBe(5001);
});

test("目标模型编译预览展示处理动作、字段和警告说明", async ({ page }) => {
  const mock = await installReverseApiMock(page, { scenario: "image_success" });
  await openStudio(page);

  await page.getByPlaceholder(/描述你想要的画面/).fill("品牌杯子广告，包含运镜和旁白");
  await page.getByLabel("提示词优化方向").selectOption("target_model_adaptation");
  await page.getByRole("button", { name: "编译到当前模型" }).click();

  const warnings = page.getByRole("list", { name: "优化风险提示" });
  await expect(warnings).toBeVisible();
  await expect(warnings.getByText("已丢弃", { exact: true })).toBeVisible();
  await expect(warnings.getByText("字段：audio.voiceover", { exact: true })).toBeVisible();
  await expect(warnings.getByText("当前图片模型不接收旁白轨。", { exact: true })).toBeVisible();
  await expect(warnings.getByText("已转换", { exact: true })).toBeVisible();
  await expect(warnings.getByText("字段：structured.camera", { exact: true })).toBeVisible();
  await expect(warnings.getByText("未验证", { exact: true })).toBeVisible();
  await expect(warnings.getByText("字段：protected.brand_text", { exact: true })).toBeVisible();
  expect(mock.promptOptimizationBodies).toHaveLength(1);
  expect(mock.promptOptimizationBodies[0].mode).toBe("target_model_adaptation");
});

test("正常多帧视频反推展示采样、证据覆盖和分析缺口", async ({ page }) => {
  const mock = await installReverseApiMock(page, { scenario: "video_success" });
  await openStudio(page);
  await page.getByRole("button", { name: /文生视频/ }).click();
  await page.locator('input[type="file"][accept*="video/mp4"]').setInputFiles(VIDEO_FILE);
  await page.getByRole("button", { name: /反推提示词.*18积分/ }).click();
  await expect(page.getByText("服务端权威报价", { exact: true })).toHaveCount(0);

  await expect(page.getByText("反推完成，已结算 18 积分（3 帧）")).toBeVisible();
  await page.getByRole("tab", { name: "分析证据" }).click();
  const evidencePanel = page.getByRole("tabpanel");
  await expect(evidencePanel.getByText("分析模式 多帧分析", { exact: true })).toBeVisible();
  await expect(evidencePanel.getByText("采样时间", { exact: true })).toBeVisible();
  await expect(evidencePanel.getByText("0.00s", { exact: true })).toBeVisible();
  await expect(evidencePanel.getByText("3.50s", { exact: true })).toBeVisible();
  await expect(evidencePanel.getByText("7.00s", { exact: true })).toBeVisible();
  await expect(evidencePanel.getByText(/视觉证据覆盖 7\.00\/8\.00s（88%）/)).toBeVisible();
  await expect(evidencePanel.getByText(/证据缺口：1 个选区尚无可验证分镜证据/)).toBeVisible();
  await expect(evidencePanel.getByText("- 7.00-8.00s 未覆盖", { exact: true })).toBeVisible();
  await expect(evidencePanel.getByText("ASR · 未请求", { exact: true })).toBeVisible();
  expect(mock.createBodies).toHaveLength(1);
  expect(mock.createBodies[0].target).toBe("video");
  expect(mock.createBodies[0].video_analysis_preset).toBe("standard");
  expect(mock.operation?.cost_settled).toBe(18);
});

test("运行中反推可幂等取消并显示退款结果", async ({ page }) => {
  const mock = await installReverseApiMock(page, { scenario: "cancel" });
  await openStudio(page);
  await startImageReverse(page);

  await expect(page.getByText("E2E 后台分析")).toBeVisible();
  await page.getByRole("button", { name: "取消反推" }).click();
  await expect(page.getByText("反推任务已取消，冻结积分已退回。")).toBeVisible();
  expect(mock.cancelRequests).toBe(1);
  expect(mock.operation?.status).toBe("canceled");
  expect(mock.operation?.cost_frozen).toBe(0);
});

test("刷新后从 V2 草稿和活动任务恢复进度并继续结算", async ({ page }) => {
  const mock = await installReverseApiMock(page, { scenario: "refresh" });
  await openStudio(page);
  await expect.poll(() => mock.draftReads).toBeGreaterThan(0);
  await expect.poll(() => mock.draftWrites, { timeout: 10_000 }).toBeGreaterThan(0);
  await uploadImageReference(page);
  await expect.poll(
    () => mock.draftPayloads.some((draft) => draft?.workspaces?.image?.selected?.id === 7001),
    { timeout: 10_000 },
  ).toBe(true);
  await expect.poll(
    () => mock.draftPayload?.workspaces?.image?.selected?.id || null,
    { timeout: 5_000 },
  ).toBe(7001);
  await expect(page.getByRole("button", { name: /image$/ })).toBeVisible();
  await page.getByRole("button", { name: /反推提示词.*2积分/ }).click();
  await expect(page.getByText("服务端权威报价", { exact: true })).toHaveCount(0);
  await expect(page.getByText("E2E 后台分析")).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const raw = window.localStorage.getItem("studio_session_draft_v1:user:9001");
    if (!raw) return null;
    return JSON.parse(raw)?.workspaces?.image?.reverseOperation?.id || null;
  })).toBe(6001);

  await page.reload();
  await expect(page.getByText("E2E 后台分析")).toBeVisible();
  mock.allowCompletion = true;

  await expect(page.getByText("反推完成，已结算 2 积分")).toBeVisible();
  expect(mock.createBodies).toHaveLength(1);
  expect(mock.operationReads).toBeGreaterThan(0);
});

test("V2 反推历史可恢复模式、素材、结构和最终文本", async ({ page }) => {
  const history = reverseHistoryV2Row();
  await installReverseApiMock(page, { history: [history] });
  await page.goto("/prompts");
  await page.getByRole("button", { name: /我的提示词/ }).click();

  const card = page.locator("article").filter({ hasText: history.title });
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "套用" }).click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByText("已恢复反推文字、结构、素材和分析证据。")).toBeVisible();
  await expect(page.getByPlaceholder(/描述你想要的画面/)).toHaveValue("V2 恢复提示词");
  await expect(page.locator("label").filter({ hasText: /^主体$/ }).locator("xpath=following-sibling::input"))
    .toHaveValue("历史 E2E 杯子");
  await expect(page.getByText("图片参考", { exact: true })).toBeVisible();
});

test.describe("移动端", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("视频抽帧失败后明确确认封面、2 积分结算和音频未分析", async ({ page }) => {
    const mock = await installReverseApiMock(page, { scenario: "cover_confirmation" });
    await openStudio(page);
    await page.getByRole("button", { name: /文生视频/ }).click();
    await page.locator('input[type="file"][accept*="video/mp4"]').setInputFiles(VIDEO_FILE);
    await page.getByRole("button", { name: /反推提示词.*18积分/ }).click();
    await expect(page.getByText("服务端权威报价", { exact: true })).toHaveCount(0);

    const confirmation = page.getByRole("alert").filter({ hasText: "视频抽帧失败" });
    await expect(confirmation).toBeVisible();
    await expect(confirmation).toContainText("确认后仅结算 2 积分");
    await expect(confirmation).toContainText("退还16 积分差额");
    await confirmation.getByRole("button", { name: "使用封面分析" }).click();

    await expect(page.getByText("反推完成，已结算 2 积分")).toBeVisible();
    await page.getByRole("tab", { name: "分析证据" }).click();
    const evidencePanel = page.getByRole("tabpanel");
    await expect(evidencePanel.getByText("分析模式 封面单帧", { exact: true })).toBeVisible();
    await expect(evidencePanel.getByText("降级原因：抽帧失败，已经用户确认使用封面", { exact: true })).toBeVisible();
    await expect(evidencePanel.getByText("采样时间", { exact: true })).toBeVisible();
    await expect(evidencePanel.getByText("0.00s", { exact: true })).toBeVisible();
    await expect(evidencePanel.getByText(/视觉证据覆盖 0\.00\/5\.00s（0%）/)).toBeVisible();
    await expect(evidencePanel.getByText(/证据缺口/).first()).toBeVisible();
    await expect(evidencePanel.getByText("ASR · 未请求", { exact: true })).toBeVisible();
    expect(mock.confirmRequests).toBe(1);
    expect(mock.operation?.cost_settled).toBe(2);
  });
});
