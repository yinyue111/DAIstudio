import assert from "node:assert/strict";
import {
  composeSafeVideoTransferPrompt,
  composeStyleTransferPrompt,
} from "../app/studio/helpers.ts";

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
  "时序分镜": "0-5s Estee Lauder 产品旋转；5-10s Advanced Night Repair Logo 特写",
  "字幕卖点": "Estee Lauder Advanced Night Repair",
  "场景背景": "暖棕色广告棚景和金色沙粒台面",
  "视角构图": "竖屏 9:16, 产品居中, 浅景深",
  "镜头运动": "缓慢推进并轻微环绕",
  "光线": "柔和电影感主光和边缘高光",
  "标签": "Estee Lauder, Advanced Night Repair, skincare bottle",
}, "", { video: true, subject: "product" });

assert.match(productVideoStylePrompt, /上传产品作为唯一视频主体/);
assert.match(productVideoStylePrompt, /替换参考片中的原主体/);
assert.match(productVideoStylePrompt, /具体动作、运镜、节奏和先后顺序/);
assert.match(productVideoStylePrompt, /镜头从左侧入场/);
assert.match(productVideoStylePrompt, /产品缓慢旋转/);
assert.match(productVideoStylePrompt, /缓慢推进并轻微环绕/);
assert.match(productVideoStylePrompt, /包装结构、Logo、可见文字、颜色和材质纹理连续一致/);
assert.doesNotMatch(productVideoStylePrompt, /避免快速旋转|稳定陈列/);
assert.doesNotMatch(productVideoStylePrompt, /Estee Lauder/i);
assert.doesNotMatch(productVideoStylePrompt, /Advanced Night Repair/i);
assert.doesNotMatch(productVideoStylePrompt, /skincare bottle/i);
assert.doesNotMatch(productVideoStylePrompt, /0-5s/i);

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

const generalVideoWithoutReferenceText = composeStyleTransferPrompt({
  "场景背景": "明亮浴室",
  "镜头运动": "缓慢推近",
  "字幕卖点": "干湿两用",
}, "", { video: true });
assert.match(generalVideoWithoutReferenceText, /明亮浴室/);
assert.doesNotMatch(generalVideoWithoutReferenceText, /干湿两用/);

const chineseIdentityLeak = composeSafeVideoTransferPrompt({
  "主体": "正面包装文字包含黑魔法全棉棉柔巾",
  "场景背景": "暖棕色广告棚景",
  "可迁移主体动作": "慢速旋转后推近包装特写",
  "迁移生成指令": "黑魔法全棉棉柔巾慢速旋转展示",
}, "product");
assert.doesNotMatch(chineseIdentityLeak, /黑魔法全棉棉柔巾/);
assert.match(chineseIdentityLeak, /慢速旋转后推近包装特写/);

const genericSceneOverlap = composeSafeVideoTransferPrompt({
  "主体": "一盒棉柔巾位于白色台面中央，包装正面可见",
  "场景背景": "白色台面与虚化绿植",
  "可迁移主体动作": "从下方抽出一张棉柔巾后推近纹理",
  "迁移生成指令": "白色台面中央，上传产品从下方抽出一张棉柔巾，随后推近展示纹理",
}, "product");
assert.equal(
  genericSceneOverlap,
  "白色台面中央，上传产品从下方抽出一张棉柔巾，随后推近展示纹理",
  "generic scene placement must not be mistaken for reference identity",
);

const identityAfterPlacementPhrase = composeSafeVideoTransferPrompt({
  "品牌Logo": "Logo位于包装正面品牌为黑魔法",
  "场景背景": "白色台面",
  "可迁移主体动作": "缓慢旋转后推近包装特写",
  "迁移生成指令": "黑魔法产品缓慢旋转",
}, "product");
assert.doesNotMatch(identityAfterPlacementPhrase, /黑魔法/);
assert.match(identityAfterPlacementPhrase, /缓慢旋转后推近包装特写/);

const safeSecondaryMotion = composeStyleTransferPrompt({
  "主体": "正面包装文字包含黑魔法全棉棉柔巾",
  "场景背景": "明亮浴室",
  "可迁移主体动作": "黑魔法全棉棉柔巾缓慢旋转",
  "主体动作": "手从包装底部抽出洗脸巾",
}, "", { video: true, subject: "product" });
assert.doesNotMatch(safeSecondaryMotion, /黑魔法全棉棉柔巾/);
assert.match(safeSecondaryMotion, /手从包装底部抽出洗脸巾/);

const quotedOverlayTransfer = composeSafeVideoTransferPrompt({
  "场景背景": "明亮浴室",
  "可迁移主体动作": "微距展开云纹",
  "迁移生成指令": "明亮浴室，微距展开云纹；文字“新品上市”浮现；慢速推近",
}, "product");
assert.match(quotedOverlayTransfer, /微距展开云纹/);
assert.match(quotedOverlayTransfer, /慢速推近/);
assert.doesNotMatch(quotedOverlayTransfer, /文字|新品上市|浮现/);

console.log("style-transfer prompt tests passed");
