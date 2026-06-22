"""Prompt reverse-engineering templates and parsing helpers."""
from __future__ import annotations

import json
import re

# Rich multi-dimension template so the regenerated image stays close to the
# reference. Keep keys stable -- the frontend renders whatever keys come back.
IMAGE_REVERSE_TEMPLATE = (
    "你是世界顶级的视觉复刻专家。请以像素级的严谨程度观察这张参考图,目标是让另一位画师或模型"
    "仅凭你的文字,就能复刻出与原图几乎无法区分的画面。\n"
    "硬性要求:必须具体、可量化,严禁『一些/可能/比较/大概』等模糊词——\n"
    "  • 颜色:给具体色名 + 十六进制色值(如 暖橙 #E8945A),并标明主色/辅色/点缀色各自的画面占比;\n"
    "  • 位置:用画面百分比坐标描述(如 主体居中偏左、约占画幅 60%、视平线在画面 45% 高度处);\n"
    "  • 数量/角度/比例:给确切数值(如 3 个人物、镜头俯角约 15°、主体与背景虚实比);\n"
    "  • 镜头:估计焦段(mm)、光圈感(景深深浅)、视角(平视/俯/仰)。\n"
    "输出严格的 JSON(不要任何额外文字、不要 markdown)。final_text 必须从下列维度逐项压缩整合而来,"
    "不得引入维度中没有出现的新主体/场景/风格;主体、细节、场景、风格、构图、景别、镜头、光线、配色、材质、氛围必须能在 final_text 中对应找到。\n"
    "字段如下:\n"
    "{\n"
    '  "主体": "主要对象:类别、确切数量、姿态、朝向、表情/神态/动作状态",\n'
    '  "细节特征": "主体可识别的关键细节:服饰/材质/造型/发型/纹样/标志性配件等,越细越好",\n'
    '  "场景背景": "环境、地点、背景元素;前景/中景/远景的层次与各自内容",\n'
    '  "风格": "确切的艺术/摄影风格与流派(如 皮克斯3D渲染/日系胶片/赛博朋克插画/产品棚拍),可点名参考美学",\n'
    '  "构图": "构图法(三分/中心/对角/框架等)、主体在画面中的百分比位置、画幅比例、留白分布、引导线",\n'
    '  "景别": "镜头景别:特写/近景/中景/全景/远景,以及主体占画幅的大致比例(决定主体离镜头远近)",\n'
    '  "视角镜头": "拍摄视角与俯仰角、估计焦段(mm)、景深(浅/深及虚化程度)、透视强弱",\n'
    '  "光线": "光源类型与方向(如 左上 45° 主光)、软硬、明暗对比、阴影形状与浓度、色温、时间氛围",\n'
    '  "色调配色": "主色/辅色/点缀色的具体色名+色值与画面占比、整体饱和度、冷暖倾向、对比强弱、是否有色彩滤镜",\n'
    '  "材质纹理": "主要表面的材质与质感(磨砂/反光/金属/布料/颗粒),以及反射/高光/粗糙度细节",\n'
    '  "氛围情绪": "画面传达的情绪与氛围基调",\n'
    '  "后期质感": "渲染/后期特征:颗粒强度、噪点、锐度、光晕、暗角强弱、色彩分级风格、是否 HDR/胶片感",\n'
    '  "文字水印": "画面中的任何文字/logo/水印的内容、字体感、位置;若无则填 无",\n'
    '  "标签": "8-15 个最能定义这张图的精炼英文关键词(主体/风格/媒介/光线/质感/质量词),逗号分隔,可直接喂给图像模型",\n'
    '  "负向": "需要明确避免的元素(多余文字/水印/多余肢体/畸变/低清/AI 痕迹等)",\n'
    '  "final_text": "按同一维度顺序整合为一段可直接用于文生图的中文提示词:主体与细节 → 场景背景 → 风格 → 景别/构图/视角镜头 → 光线/色调配色 → 材质纹理 → 氛围/后期质感。'
    "必须覆盖所有核心维度,不得与上述字段矛盾;末尾追加英文标签关键词和质量词,控制在 120-180 个中文字符。\"\n"
    "}"
)

# Video template captures temporal / motion dimensions so the model can produce
# a coherent (not static) clip. Reverse runs on sampled keyframes when possible.
VIDEO_REVERSE_TEMPLATE = (
    "你是世界顶级的商业广告导演、剪辑师和视频提示词工程师。下面按时间先后给你若干帧(从一段"
    "参考视频中等间隔抽样,第 1 张为首帧),请把它们当作同一条广告片的时间序列来分析。目标不是"
    "泛化成同类视频,而是最大限度复刻参考片的商业视觉:主体、服装/商品、模特动作、场景、构图、"
    "镜头语言、光线、色彩、字幕/卖点和剪辑节奏都要贴近原片。\n"
    "硬性要求:\n"
    "1. 只能描述你从帧中能确认或高置信推断的内容;不确定处写『未见/不确定』,不要编造新商品、新场景或新品牌。\n"
    "2. 颜色给具体色名+十六进制色值,位置用画面百分比,动作/运镜按时间顺序拆解。\n"
    "3. 如果是女装/电商广告,必须记录服装版型、面料质感、穿搭层次、模特姿态、卖点字幕、商品展示方式。\n"
    "4. final_text 必须可直接用于文生视频,以『参考片复刻』为核心,不要写成普通美图描述。\n"
    "输出严格的 JSON(不要任何额外文字、不要 markdown)。final_text 必须从下列维度逐项整合而来,"
    "不得引入维度中没有出现的新主体/场景/风格/动作;主体、商品/服装、场景、风格、视角构图、动作、运镜、分镜、字幕卖点、光线、配色必须能在 final_text 中对应找到。\n"
    "字段如下:\n"
    "{\n"
    '  "主体": "主要对象:类别、数量、性别/年龄段/体态、外观特征、初始位置和朝向",\n'
    '  "商品服装": "商品或服装的具体类别、颜色色值、版型、剪裁、长度、面料、纹理、搭配单品、配饰;若非商品广告也按可见物体写",\n'
    '  "细节特征": "可识别关键细节:妆发、鞋包、道具、logo、花纹、扣子、褶皱、反光、手部动作等",\n'
    '  "场景背景": "地点/空间、前中后景层次、地面/墙面/家具/道具、背景虚实、画面留白",\n'
    '  "广告目标": "这条片子在卖什么/展示什么卖点;若只看到画面无法确认,写未见明确卖点",\n'
    '  "风格": "确切商业影像风格(如 抖音女装种草/电商棚拍/街拍广告/直播切片/品牌大片),不要泛写电影感",\n'
    '  "视角构图": "每个主要镜头的景别、视角、主体占比、画面百分比位置、横竖画幅、留白和引导线",\n'
    '  "主体动作": "主体动作按时间顺序拆解:走位、转身、摆裙、抬手、看镜头、拿商品、切换姿势等,给方向和幅度",\n'
    '  "镜头运动": "运镜方式(推/拉/摇/移/跟/环绕/升降/手持/固定),方向、速度、幅度、是否有变焦或景深变化",\n'
    '  "剪辑节奏": "镜头数量、切换节奏、每镜头大致秒数、是否卡点、是否慢动作/加速、是否循环",\n'
    '  "时序分镜": "按 0-1s、1-2s 或镜头1/2/3 写画面演变,必须对应所见帧顺序",\n'
    '  "字幕卖点": "画面中文字/logo/价格/促销/卖点文案的内容、位置、字体风格、颜色;若无则填 无",\n'
    '  "时长建议": "建议生成时长、帧率感、是否可扩展为长视频循环段落",\n'
    '  "光线": "主光/辅光方向、软硬、色温、阴影形状、反光、高光、是否随镜头变化",\n'
    '  "色调配色": "主色/辅色/点缀色的色值和画面占比、冷暖、饱和度、对比度、调色滤镜",\n'
    '  "材质纹理": "服装/商品/皮肤/背景主要材质与质感,包括布料垂坠、反光、粗糙度、颗粒",\n'
    '  "氛围情绪": "广告气质和情绪:高级/甜美/通勤/轻奢/活力/松弛等,必须贴合画面",\n'
    '  "转场": "转场方式:硬切/闪白/遮挡/变焦/动作匹配/无,以及出现位置",\n'
    '  "一致性约束": "生成时必须保持不变的元素:主体数量、服装颜色版型、场景、画幅、字幕卖点、镜头顺序等",\n'
    '  "负向": "需要避免的元素:换脸、换衣服颜色、商品漂移、字幕乱字、水印、肢体畸变、闪烁、形变、镜头抖动、拼接感、不自然走路",\n'
    '  "final_text": "按同一维度顺序整合为一段可直接用于文生视频的中文提示词:参考片复刻 → 主体/商品服装/细节 → 场景/广告目标/风格 → 视角构图 → 动作和运镜 → 剪辑节奏/时序分镜/字幕卖点 → 光线/配色/材质/氛围 → 一致性约束。'
    '必须包含时间推进、镜头顺序、商品服装细节和字幕卖点约束,不得与上述字段矛盾;末尾追加英文视频关键词,控制在 280-420 个中文字符。"\n'
    "}"
)


def reverse_template(target: str, n_frames: int = 1) -> str:
    if target == "video":
        if n_frames > 1:
            return f"(以下共 {n_frames} 帧,按时间先后排列)\n" + VIDEO_REVERSE_TEMPLATE
        return VIDEO_REVERSE_TEMPLATE
    return IMAGE_REVERSE_TEMPLATE


def parse_structured(content: str) -> dict:
    # tolerate ```json fences / surrounding prose
    m = re.search(r"\{.*\}", content, re.S)
    raw = m.group(0) if m else content
    try:
        obj = json.loads(raw)
    except Exception:
        obj = {"主体": content.strip()[:200], "final_text": content.strip()}
    final_text = obj.pop("final_text", None) or compose_final(obj)
    return {"structured": obj, "final_text": final_text}


def compose_final(obj: dict) -> str:
    """Fallback final prompt when the model omits final_text."""
    neg = obj.get("负向")
    order = [
        "主体", "细节特征", "场景背景", "风格", "景别", "构图", "视角镜头", "视角构图",
        "主体动作", "镜头运动", "运动节奏", "时序分镜", "时长建议", "光线", "色调配色",
        "材质纹理", "氛围情绪", "后期质感", "转场", "文字水印", "标签",
    ]
    used: set[str] = set()
    parts: list[str] = []
    for key in order:
        value = obj.get(key)
        if value and key not in ("负向", "final_text"):
            parts.append(f"{key}: {value}")
            used.add(key)
    for key, value in obj.items():
        if key not in used and key not in ("负向", "final_text") and value:
            parts.append(f"{key}: {value}")
    text = "；".join(parts)
    if neg:
        text += f"；避免: {neg}"
    return text or "same style, high quality"


def mock_reverse(target: str = "image") -> dict:
    if target == "video":
        structured = {
            "主体": "示例主体(mock)", "场景背景": "简洁纯色背景", "风格": "极简插画",
            "视角构图": "正面平视,中景,主体居中约占 50%",
            "主体动作": "缓缓转身并微笑", "镜头运动": "缓慢推近(dolly-in)",
            "运动节奏": "舒缓", "时序分镜": "0-2s 静止特写;2-4s 推近;4-6s 主体动作",
            "时长建议": "6 秒 / 24fps", "光线": "柔和顶光,逐渐变亮",
            "色调配色": "暖色低饱和", "氛围情绪": "宁静治愈", "转场": "无",
            "负向": "闪烁, 形变, 拼接感, 水印",
        }
        return {
            "structured": structured,
            "final_text": (
                "minimal illustration, subject slowly turns and smiles, "
                "slow dolly-in, soft warm light, calm mood, 6s 24fps, "
                "smooth coherent motion (mock)"
            ),
        }
    structured = {
        "主体": "示例主体(mock),单个,居中正面", "细节特征": "简约造型,无明显纹样",
        "场景背景": "纯色背景,层次简单", "风格": "极简扁平插画",
        "构图": "居中,1:1,适度留白", "景别": "中近景,主体约占画幅 55%",
        "视角镜头": "正面平视,中焦,浅景深",
        "光线": "柔和顶光,低对比", "色调配色": "主色暖橙 #E8945A、辅色米白,低饱和",
        "材质纹理": "哑光纸质,细微颗粒", "氛围情绪": "宁静",
        "后期质感": "轻微胶片颗粒,柔和暗角",
        "标签": "minimal, flat illustration, centered subject, warm palette, soft light, matte texture, serene, high quality",
        "负向": "文字, 水印, 多余肢体, 畸变",
    }
    return {
        "structured": structured,
        "final_text": (
            "minimal flat illustration, single centered subject, front view, "
            "warm muted palette (orange + cream), soft top light, matte paper "
            "texture, subtle film grain, serene mood, high quality (mock)"
        ),
    }
