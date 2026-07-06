import assert from "node:assert/strict";
import { composeStyleTransferPrompt } from "../app/studio/helpers.ts";

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

const productStylePrompt = composeStyleTransferPrompt({
  "主体": "一瓶 Estee Lauder Advanced Night Repair 精华液",
  "商品服装": "棕色玻璃滴管瓶, Estee Lauder Logo 清晰",
  "材质纹理": "amber glass, dropper bottle, gold cap",
  "场景背景": "暖棕色渐变背景, 金色沙粒台面",
  "广告目标": "高端护肤品社媒广告",
  "风格": "luxury beauty ad, premium texture",
  "构图": "产品居中, 浅景深, 背景大面积留白",
  "光线": "柔和电影感主光, 边缘高光",
  "标签": "Estee Lauder, Advanced Night Repair, skincare bottle, amber glass",
}, "", { subject: "product" });

assert.match(productStylePrompt, /暖棕色渐变背景/);
assert.match(productStylePrompt, /高端护肤品社媒广告/);
assert.match(productStylePrompt, /柔和电影感主光/);
assert.doesNotMatch(productStylePrompt, /Estee Lauder/i);
assert.doesNotMatch(productStylePrompt, /Advanced Night Repair/i);
assert.doesNotMatch(productStylePrompt, /dropper bottle/i);
assert.doesNotMatch(productStylePrompt, /amber glass/i);

const productVideoStylePrompt = composeStyleTransferPrompt({
  "主体": "参考视频中的 Estee Lauder 棕色滴管瓶",
  "商品服装": "Advanced Night Repair bottle, gold cap",
  "可迁移主体动作": "镜头从左侧入场，产品缓慢旋转，水花飞溅后切到瓶身特写",
  "主体动作": "参考商品转动并出现品牌 Logo 特写",
  "场景背景": "暖棕色广告棚景和金色沙粒台面",
  "视角构图": "竖屏 9:16, 产品居中, 浅景深",
  "镜头运动": "缓慢推进并轻微环绕",
  "光线": "柔和电影感主光和边缘高光",
  "标签": "Estee Lauder, Advanced Night Repair, skincare bottle",
}, "", { video: true, subject: "product" });

assert.match(productVideoStylePrompt, /上传产品作为唯一视频主体/);
assert.match(productVideoStylePrompt, /替换参考片中的原主体/);
assert.match(productVideoStylePrompt, /展示节奏/);
assert.match(productVideoStylePrompt, /缓慢推进并轻微环绕/);
assert.doesNotMatch(productVideoStylePrompt, /Estee Lauder/i);
assert.doesNotMatch(productVideoStylePrompt, /Advanced Night Repair/i);
assert.doesNotMatch(productVideoStylePrompt, /skincare bottle/i);

const videoPrompt = composeStyleTransferPrompt({
  "主体": "参考视频里的女模特",
  "人像意图": "参考视频的人物角色设定",
  "人物比例": "参考人物头身比和腿长比例",
  "体态线条": "参考人物的S形姿态",
  "服装结构": "参考人物的红裙剪裁",
  "服装覆盖": "参考人物的服装覆盖范围",
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
assert.doesNotMatch(videoPrompt, /头身比/);
assert.doesNotMatch(videoPrompt, /S形姿态/);
assert.doesNotMatch(videoPrompt, /服装覆盖范围/);

console.log("style-transfer prompt tests passed");
