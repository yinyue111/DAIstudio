import assert from "node:assert/strict";
import { composeStyleTransferPrompt } from "../app/studio/helpers.js";

const imagePrompt = composeStyleTransferPrompt({
  "主体": "红色手提包, 带参考品牌 Logo",
  "细节特征": "参考商品的金属扣和包装文字",
  "场景背景": "白色摄影棚, 大理石台面",
  "风格": "小红书高级产品广告",
  "构图": "主体居中偏右, 画面 60% 留白",
  "光线": "左上 45 度柔光, 高光干净",
  "色调配色": "奶白 #F7F2E8 与浅金 #D8B56D",
  "文字水印": "参考图促销文案",
});

assert.match(imagePrompt, /白色摄影棚/);
assert.match(imagePrompt, /小红书高级产品广告/);
assert.match(imagePrompt, /左上 45 度柔光/);
assert.doesNotMatch(imagePrompt, /红色手提包/);
assert.doesNotMatch(imagePrompt, /参考品牌 Logo/);
assert.doesNotMatch(imagePrompt, /参考图促销文案/);

const videoPrompt = composeStyleTransferPrompt({
  "主体": "参考视频里的女模特",
  "商品服装": "参考视频里的红裙",
  "场景背景": "街边橱窗与暖色灯箱",
  "视角构图": "竖屏 9:16, 中近景跟拍",
  "主体动作": "参考模特转身摆裙",
  "镜头运动": "缓慢前推并轻微跟随",
  "剪辑节奏": "每 2 秒一个镜头",
  "字幕卖点": "无",
  "光线": "傍晚逆光与柔和轮廓光",
}, "", { video: true });

assert.match(videoPrompt, /街边橱窗/);
assert.match(videoPrompt, /竖屏 9:16/);
assert.match(videoPrompt, /缓慢前推/);
assert.doesNotMatch(videoPrompt, /女模特/);
assert.doesNotMatch(videoPrompt, /红裙/);
assert.doesNotMatch(videoPrompt, /转身摆裙/);

console.log("style-transfer prompt tests passed");
