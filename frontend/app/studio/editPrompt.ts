const EDIT_PROTECTION_NEGATIVE = [
  "logo变形",
  "文字乱码",
  "标签不可读",
  "标签文字不可读",
  "包装被篡改",
  "比例失真",
  "错误品牌文字",
  "品牌名错误",
  "商标错误",
  "包装文字变化",
  "包装文字错字",
  "包装文字缺失",
  "文字被翻译",
  "文字新增",
  "文字删减",
  "标签位置改变",
  "产品变形",
];

const PRODUCT_EDIT_NEGATIVE = [
  "logo扭曲",
  "logo丢失",
  "Logo重绘",
  "Logo字体变化",
  "包装文字乱码",
  "包装文字被改写",
  "产品正面文字被重排",
  "顶部文字被改写",
  "品牌名被替换",
  "图案变形",
  "包装纹理丢失",
  "产品外形改变",
  "产品颜色漂移",
  "标签缺失",
  "贴纸变形",
  "材质错误",
  "主体被替换",
  "多余商品",
  "低清产品",
  "边缘糊化",
];

const PORTRAIT_EDIT_NEGATIVE = [
  "换脸失败",
  "五官变形",
  "脸型变化",
  "身份不一致",
  "年龄变化",
  "性别变化",
  "肤色漂移",
  "发型错误",
  "头发边缘糊化",
  "眼睛不对称",
  "表情僵硬",
  "手指畸形",
  "肢体畸形",
  "多余人物",
  "人物消失",
  "主体被替换",
  "脸部低清",
  "面部涂抹感",
  "未成年感",
  "幼态成人化",
  "不自然亲密姿态",
  "低机位身体凝视",
  "身体局部特写",
  "过度裸露",
  "私密暧昧",
  "湿身成人化",
];

export function buildEditPrompt(baseText, {
  video = false,
  hasStyleReference = false,
  generalEdit = false,
  subject = "product",
} = {}) {
  const base = String(baseText || "").trim() || "生成同风格商业素材";
  const isPortrait = subject === "portrait";
  const styleScope = hasStyleReference
    ? (isPortrait
        ? "风格参考只用于迁移场景、构图、镜头语言、光线、色调、服化道、广告质感、氛围和动作节奏；不要迁移风格参考里的人脸身份、五官、具体人物、品牌、Logo、文字水印或促销文案。"
        : "风格参考只用于迁移场景、构图、镜头语言、光线、色调、材质、广告质感和氛围；不要迁移风格参考里的主体、人物、商品、品牌、Logo、包装、文字水印或促销文案。")
    : "";
  if (generalEdit) {
    return `以用户上传的图片作为唯一编辑源，严格按照用户提示词执行局部或整体编辑；保留未被要求修改的主体、构图、Logo、文字、颜色、比例和关键细节，不要无故替换主体或品牌。${styleScope}编辑要求：${base}`;
  }
  if (isPortrait) {
    const guard = video
      ? "以用户上传的人像照片作为唯一人物身份参考和主体身份锁定，目标视频/风格参考只用于迁移镜头语言、动作节奏、场景、服化道、光线、色调和广告质感；必须保持同一个人的脸型、五官比例、发型特征、肤色、年龄感、性别、体态和可识别身份，不替换、不混合、不把参考视频里的人物身份迁移过来；"
      : "以用户上传的人像照片作为唯一人物身份和编辑源，必须保持同一个人的脸型、五官比例、发型特征、肤色、年龄感、性别、体态和可识别身份；只迁移参考素材的场景、构图、光线、色调、妆造氛围、广告质感和画面风格，不替换、不混合、不重绘成另一个人；";
    const fidelity = "人脸必须清晰自然，眼睛、鼻子、嘴型、脸型轮廓、发际线和标志性特征保持一致；整体表达保持成年、自然、得体、专业商业人像/品牌 Lookbook/角色设定语境，重点表现气质、服装结构、姿态、镜头和光影，不做身体局部凝视、夸张身体展示姿态、私密成人化或未成年感表达；若风格迁移和人物身份保真冲突，优先保证人物身份、面部结构和自然表情完全稳定。";
    return `${guard}${styleScope}${fidelity}生成高质量人像/商业视觉素材。迁移要求：${base}`;
  }
  const guard = video
    ? "以用户上传的产品主体图作为视频首帧和唯一产品身份参考，必须用上传产品替换参考视频中的原主体、原商品、原品牌或人物；参考视频只提供场景、构图、光线、镜头运动、剪辑节奏、展示动作和广告质感。必须完整保留上传产品主体、Logo、包装、颜色、形状、材质、比例、文字标识和品牌身份；产品表面像素视为锁定图层，不替换、不重绘、不改款、不改品牌；"
    : "以用户上传的产品图作为唯一产品身份和编辑源，必须完整保留同一 SKU 的主体、Logo、包装结构、品牌色、形状轮廓、材质纹理、比例、标签版式、文字标识和可识别细节；产品表面像素视为锁定图层，不替换、不重绘、不改款、不改品牌；";
  const fidelity = "包装上的品牌名、Logo、中文、英文、韩文、数字、装饰图案、标签位置和排版必须逐字逐形保持原图，不得翻译、改写、补写、删减、重排、风格化、模糊或替换；产品主体必须清晰锐利，边缘自然融入新场景；只允许迁移或生成背景、台面、道具、光线、构图、广告氛围、画面质感和可适配到产品的展示动作，不得把参考素材里的商品、人物、服装或品牌覆盖到上传产品上。若风格迁移和产品保真冲突，优先保证产品主体与包装文字完全不变。";
  return `${guard}${styleScope}${fidelity}生成广告级商业素材。迁移要求：${base}`;
}

export function buildEditNegativePrompt(value, { productMode = false, portraitMode = false, editMode = true } = {}) {
  const parts = String(value || "")
    .split(/[,，、\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
  if (!editMode) return parts.join("，");
  const seen = new Set(parts.map((item) => item.toLowerCase()));
  for (const item of [
    ...EDIT_PROTECTION_NEGATIVE,
    ...(productMode ? PRODUCT_EDIT_NEGATIVE : []),
    ...(portraitMode ? PORTRAIT_EDIT_NEGATIVE : []),
  ]) {
    if (!seen.has(item.toLowerCase())) {
      seen.add(item.toLowerCase());
      parts.push(item);
    }
  }
  return parts.join("，");
}
