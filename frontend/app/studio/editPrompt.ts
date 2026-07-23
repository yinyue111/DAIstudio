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
  "产品残缺",
  "半截产品",
  "产品被裁切",
  "产品主体出画",
  "只显示产品局部",
  "包装边缘缺失",
  "顶部缺失",
  "底部缺失",
  "左右边缘缺失",
  "抽口缺失",
  "盖子缺失",
  "提手缺失",
  "盒体破损",
  "盒体压扁",
  "包装结构改变",
  "产品被草叶遮挡",
  "产品被前景遮挡",
  "道具遮挡Logo",
  "道具遮挡包装文字",
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
        ? "风格参考只用于迁移场景、构图、镜头、光线、色调、服化道和氛围，不迁移其中的人物身份或文字。"
        : "风格参考只用于迁移场景、构图、镜头、光线、色调、材质和后期质感，不迁移其中的主体、品牌或文字。")
    : "";
  if (generalEdit) {
    return `以用户上传的图片作为唯一编辑源，严格按照用户提示词执行局部或整体编辑；保留未被要求修改的主体、构图、Logo、文字、颜色、比例和关键细节，不要无故替换主体或品牌。${styleScope}编辑要求：${base}`;
  }
  if (isPortrait) {
    const guard = video
      ? "上传人像是唯一人物身份参考；保持脸型、五官、发型、肤色、年龄感、体态与可识别身份，不迁移参考视频里的人物身份；"
      : "上传人像是唯一人物身份和编辑源；保持脸型、五官、发型、肤色、年龄感、体态与可识别身份，不替换或混合人物；";
    const fidelity = "人脸清晰自然，保持服装覆盖下可见的身体轮廓、比例与姿态；表达成年、自然、得体的专业商业人像。冲突时人物身份和身体比例优先。";
    return `${guard}${styleScope}${fidelity}生成商业人像。迁移要求：${base}`;
  }
  const guard = video
    ? "上传产品是唯一产品身份，用上传产品替换参考视频中的原主体；保持同一SKU的外形、比例、包装、材质、Logo和可见文字，不改款或改品牌；"
    : "上传产品是唯一产品身份和编辑源；保持同一SKU的外形、比例、包装结构、品牌色、材质、Logo和可见文字，不替换、重绘或改款；";
  const fidelity = `产品清晰完整入镜，不裁切、遮挡或变形；只改变背景、道具、光线、构图、材质表现和后期质感。${video ? "允许可适配到产品的展示动作，避免快速旋转和运动模糊；" : ""}冲突时产品与包装文字保真优先。`;
  return `${guard}${styleScope}${fidelity}生成商业产品图。迁移要求：${base}`;
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
