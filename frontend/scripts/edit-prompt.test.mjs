import assert from "node:assert/strict";

import { buildEditNegativePrompt, buildEditPrompt } from "../app/studio/editPrompt.ts";

const productPrompt = buildEditPrompt("小红书浴室场景，柔光，干净广告质感", {
  subject: "product",
  hasStyleReference: true,
});

assert.match(productPrompt, /唯一产品身份/);
assert.match(productPrompt, /Logo和可见文字/);
assert.match(productPrompt, /完整入镜/);
assert.match(productPrompt, /只用于迁移场景/);
assert.match(productPrompt, /迁移要求：小红书浴室场景/);
assert.doesNotMatch(productPrompt, /生成广告级|高质量人像/);
assert.ok(productPrompt.length <= 450, `product prompt too long: ${productPrompt.length}`);

const portraitVideoPrompt = buildEditPrompt("参考视频的走位和镜头节奏", {
  subject: "portrait",
  video: true,
  hasStyleReference: true,
});

assert.match(portraitVideoPrompt, /唯一人物身份参考/);
assert.match(portraitVideoPrompt, /不迁移参考视频里的人物身份/);
assert.match(portraitVideoPrompt, /人物身份和身体比例/);
assert.doesNotMatch(portraitVideoPrompt, /生成广告级|高质量人像/);
assert.ok(portraitVideoPrompt.length <= 450, `portrait prompt too long: ${portraitVideoPrompt.length}`);

const generalPrompt = buildEditPrompt("把背景换成纯白棚拍", { generalEdit: true });
assert.match(generalPrompt, /严格按照用户提示词执行/);
assert.match(generalPrompt, /保留未被要求修改的主体/);

const productNegative = buildEditNegativePrompt("文字乱码，产品变形", { productMode: true });
assert.match(productNegative, /文字乱码/);
assert.match(productNegative, /产品变形/);
assert.match(productNegative, /包装文字被改写/);
assert.match(productNegative, /产品残缺/);
assert.match(productNegative, /半截产品/);
assert.match(productNegative, /产品被裁切/);
assert.equal(productNegative.split("，").filter((item) => item === "文字乱码").length, 1);

const portraitNegative = buildEditNegativePrompt("", { portraitMode: true });
assert.match(portraitNegative, /身份不一致/);
assert.match(portraitNegative, /脸部低清/);

const passthroughNegative = buildEditNegativePrompt("低清, 模糊", { editMode: false, productMode: true });
assert.equal(passthroughNegative, "低清，模糊");

console.log("edit prompt tests passed");
