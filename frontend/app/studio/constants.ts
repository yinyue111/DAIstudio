export const CREATION_MODES = [
  { key: "image", label: "文生图", icon: "✦" },
  { key: "image_edit", label: "图片编辑", icon: "◐" },
  { key: "video", label: "文生视频", icon: "▶" },
  { key: "video_edit", label: "图生视频", icon: "▣" },
];

export const EDIT_STYLE_KEYS = {
  image: ["场景背景", "广告目标", "风格", "构图", "景别", "视角镜头", "视角构图", "光线", "色调配色", "材质纹理", "氛围情绪", "后期质感", "标签"],
  video: ["场景背景", "广告目标", "风格", "视角构图", "镜头运动", "剪辑节奏", "时序分镜", "字幕卖点", "光线", "色调配色", "材质纹理", "氛围情绪", "转场", "时长建议", "后期质感", "标签"],
};

export function creationModeLabel(mode) {
  return CREATION_MODES.find((item) => item.key === mode)?.label || "生成";
}

export const RATIOS = [
  { key: "1:1", label: "1:1", hint: "头像 / 方图", w: 1, h: 1 },
  { key: "4:5", label: "4:5", hint: "社媒竖图", w: 4, h: 5 },
  { key: "5:4", label: "5:4", hint: "商品横图", w: 5, h: 4 },
  { key: "3:4", label: "3:4", hint: "竖版海报", w: 3, h: 4 },
  { key: "4:3", label: "4:3", hint: "横版构图", w: 4, h: 3 },
  { key: "2:3", label: "2:3", hint: "封面 / 写真", w: 2, h: 3 },
  { key: "3:2", label: "3:2", hint: "摄影横图", w: 3, h: 2 },
  { key: "9:16", label: "9:16", hint: "手机竖屏", w: 9, h: 16 },
  { key: "16:9", label: "16:9", hint: "宽屏视频封面", w: 16, h: 9 },
  { key: "21:9", label: "21:9", hint: "超宽横幅", w: 21, h: 9 },
  { key: "9:21", label: "9:21", hint: "长竖海报", w: 9, h: 21 },
];

export const VIDEO_RATIO_KEYS = new Set(["1:1", "3:4", "4:3", "9:16", "16:9"]);

export const IMAGE_QUALITY_PRESETS = [
  { key: "1k", label: "标准", hint: "快速生成", maxSide: 1024 },
  { key: "2k", label: "2K", hint: "按 2K 请求，展示网关实际返回图", maxSide: 2048 },
];

export const VIDEO_QUALITIES = [
  { key: "480p", label: "480p", hint: "快速预览" },
  { key: "720p", label: "720p", hint: "标准" },
  { key: "1080p", label: "1080p", hint: "高清" },
];

export const MAX_VIDEO_DURATION_SECONDS = 15;

export const VIDEO_DURATION_PRESETS = [
  { seconds: 5, label: "5s", hint: "短镜头预览" },
  { seconds: 8, label: "8s", hint: "平台常用短片" },
  { seconds: 10, label: "10s", hint: "完整短镜头" },
  { seconds: 15, label: "15s", hint: "广告片段" },
];

export const TERMINAL_TASK_STATUSES = new Set(["succeeded", "failed", "needs_review", "canceled"]);
export const PARSE_POLL_INTERVAL_MS = 1000;
export const PARSE_POLL_TIMEOUT_MS = 240_000;
export const STUDIO_SESSION_DRAFT_KEY = "studio_session_draft_v1";
export const STUDIO_VARIATION_DRAFT_KEY = "studio_variation_draft_v1";

export const EXAMPLES = [
  "赛博朋克城市夜景，霓虹灯反射在湿漉漉的街道上，电影感，超广角",
  "一只穿宇航服的柴犬漂浮在太空，背景是绚丽星云，3D 渲染，皮克斯风格",
  "极简北欧风咖啡馆，晨光透过落地窗，暖色调，柔和景深",
  "国潮水墨山水，仙鹤掠过云海，金箔点缀，高级感海报",
];
