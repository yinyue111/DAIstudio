import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import {
  EXPORTED_MEDIA_BYTES,
  installProjectExportApiMock,
  readStoredZip,
} from "./fixtures/project-export-api";

test.beforeEach(async ({ context, baseURL }) => {
  if (!baseURL) throw new Error("Playwright baseURL is required");
  await context.addCookies([{
    name: "ai_studio_token",
    value: "project-export-e2e-session",
    url: baseURL,
    httpOnly: true,
    sameSite: "Lax",
  }]);
});

test("项目导出下载 ZIP，清单与授权媒体一致且未授权素材不泄漏", async ({ page }) => {
  const mock = await installProjectExportApiMock(page);

  await page.goto("/projects?project=501");
  await expect(page.getByRole("heading", { name: "E2E 安全导出项目" })).toBeVisible();

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出 ZIP", exact: true }).click();
  const download = await downloadPromise;

  expect(mock.exportRequests).toEqual([{ method: "GET", includeMedia: true }]);
  expect(download.suggestedFilename()).toBe("project-501-safe-export.zip");

  const downloadPath = await download.path();
  expect(downloadPath).not.toBeNull();
  const entries = readStoredZip(await readFile(downloadPath!));
  expect([...entries.keys()]).toEqual(expect.arrayContaining([
    "project.json",
    "tasks/tasks.json",
    "drafts/project-draft.json",
    "recipes/index.json",
    "assets/manifest.json",
    "media/001-allowed.png",
  ]));

  const manifest = JSON.parse(entries.get("assets/manifest.json")!.toString("utf8"));
  expect(manifest).toEqual(expect.arrayContaining([
    expect.objectContaining({ asset_ref: "g.7101", media_included: true, media_path: "media/001-allowed.png", media_exclusion_reason: null }),
    expect.objectContaining({ asset_ref: "g.7102", media_included: false, media_path: null, media_exclusion_reason: "原始媒体未授权" }),
    expect.objectContaining({ asset_ref: "g.7103", media_included: false, media_path: null, media_exclusion_reason: "素材不存在或已过期" }),
  ]));
  expect(entries.get("media/001-allowed.png")).toEqual(EXPORTED_MEDIA_BYTES);
  const mediaEntries = [...entries.keys()].filter((name) => name.startsWith("media/"));
  expect(mediaEntries).toEqual(["media/001-allowed.png"]);
  expect(Buffer.concat(mediaEntries.map((name) => entries.get(name)!)).toString("utf8")).not.toContain("locked");
  expect(Buffer.concat(mediaEntries.map((name) => entries.get(name)!)).toString("utf8")).not.toContain("expired");

  await expect(page.getByText("已导出 project-501-safe-export.zip", { exact: true })).toBeVisible();
});
