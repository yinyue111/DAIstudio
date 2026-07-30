from app.models import GenTask
from app.services import generation_video_submit
from app.services.video_prompt_compiler import (
    build_video_prompt_references,
    compile_video_prompt,
    infer_video_model_profile,
)

DIRECT_PRODUCT_VIDEO_PROMPT = (
    "视频风格要求是高端日系的美妆个护拍摄风格，强调自然、亲肤、安心；"
    "画面以粉绿、米白、奶油色系为主调，冷暖平衡，偏冷但不刺眼；"
    "镜头多用特写、微距、中景，慢动作加轻微推拉，稳定器拍摄感；"
    "场景为真实家庭浴室。产品外包装长20cm宽10cm高24cm，洗脸巾20cm见方，"
    "纹路为3D如意云纹，包装图和洗脸巾纹路与上传产品图保持相同。"
    "女主走到悬挂包装处，从下方抽出一张洗脸巾；推近放大纹理，手指捏住边缘缓慢展开，"
    "双手对折再展开展示厚度；俯拍洗脸巾浸水并形成扩散波纹，文字“干湿两用”浮现，水滴落水声；"
    "双手拧干，水流飞溅，文字“厚实吸水”出现；侧脸特写，洗脸巾轻拭脸颊，文字“亲肤”浮现；"
    "展开洗脸巾擦拭手臂，展示背面压纹，文字“加大加厚 克重95g/㎡”浮现；"
    "双手撕开展示无弹力纤维，文字“耐拉扯 不易掉絮”浮现；"
    "最后包装置于白色圆桌中央，周围为香薰棒、毛巾和白玫瑰；"
    "温柔女声旁白：“让洗脸这件事，成为一天温柔的开始。”"
)


def test_direct_product_video_passthrough_separates_post_production_from_visual_prompt():
    result = compile_video_prompt(
        {
            "user_instruction": DIRECT_PRODUCT_VIDEO_PROMPT,
            "raw_text": DIRECT_PRODUCT_VIDEO_PROMPT,
            "assembled_text": DIRECT_PRODUCT_VIDEO_PROMPT,
            "final_text": DIRECT_PRODUCT_VIDEO_PROMPT,
            "产品身份档案": "冗长OCR档案不应注入直通提示词；Moon Logo；额外包装描述。",
        },
        duration=10,
        model_id="doubao-seedance-2-0-mini-260615",
        provider="volcengine_ark",
        references=[{"role": "product", "url": "https://example.com/product.png"}],
        product_reference=True,
        product_lock_mode="locked",
        product_video_template="prompt_driven",
        fit_mode="single_clip",
    )

    assert result["prompt"].startswith(
        "产品身份约束：以上传产品图为唯一商品主体，保持同一SKU的包装结构、Logo、"
        "包装文字、颜色、材质和纹理一致。\n原始生成要求：\n"
    )
    assert "女主走到悬挂包装处，从下方抽出一张洗脸巾" in result["prompt"]
    assert "最后包装置于白色圆桌中央" in result["prompt"]
    for selling_point in (
        "干湿两用",
        "厚实吸水",
        "亲肤",
        "加大加厚 克重95g/㎡",
        "耐拉扯 不易掉絮",
    ):
        assert f"文字“{selling_point}”" not in result["prompt"]
    for overlay_only in ("干湿两用", "厚实吸水", "加大加厚 克重95g/㎡", "耐拉扯 不易掉絮"):
        assert overlay_only not in result["prompt"]
    assert "水滴落水声" not in result["prompt"]
    assert "让洗脸这件事，成为一天温柔的开始" not in result["prompt"]
    assert "画面无字" not in result["prompt"]
    assert "Shot " not in result["prompt"]
    assert "Moon Logo" not in result["prompt"]
    assert result["plan"]["shots"] == [result["prompt"].split("原始生成要求：\n", 1)[1]]
    assert result["plan"]["post_overlays"] == [
        "干湿两用",
        "厚实吸水",
        "亲肤",
        "加大加厚 克重95g/㎡",
        "耐拉扯 不易掉絮",
    ]
    assert result["plan"]["voiceover"] == "让洗脸这件事，成为一天温柔的开始。"
    assert result["plan"]["sfx"] == ["水滴落水声"]
    assert result["metadata"]["prompt_mode"] == "direct_passthrough"
    assert result["metadata"]["shot_count"] == 1


def test_timed_direct_prompt_counts_shots_and_embeds_seedance_15_audio_requirements():
    raw_text = (
        "10秒高端个护广告。\n"
        "Shot 1（0.00-2.00s）：人物伸懒腰后硬切。\n"
        "Shot 2（2.00-6.00s）：洗脸巾浸水，画面依次浮现“干湿两用”“厚实吸水”。\n"
        "Shot 3（6.00-10.00s）：擦拭脸颊，画面文字“亲肤”；"
        "画面文字仅出现上述三组卖点；音效：水滴声和纸巾摩擦声。"
    )
    prompt = {
        "input_mode": "direct_input",
        "user_instruction": raw_text,
        "raw_text": raw_text,
        "assembled_text": raw_text,
        "final_text": raw_text,
    }

    result = compile_video_prompt(
        prompt,
        duration=10,
        model_id="doubao-seedance-1-5-pro-251215",
        provider="volcengine_ark",
        fit_mode="single_clip",
    )

    assert result["metadata"]["shot_count"] == 3
    assert result["metadata"]["source_shot_count"] == 3
    assert result["metadata"]["embedded_av_requirements"] is True
    assert result["metadata"]["embedded_text_overlays"] is False
    assert result["prompt"].count("Shot ") == 3
    assert result["plan"]["post_overlays"] == ["干湿两用", "厚实吸水", "亲肤"]
    assert "仅出现上述三组卖点" not in result["plan"]["post_overlays"]
    assert result["plan"]["sfx"] == ["水滴声和纸巾摩擦声"]
    assert "音画生成要求" in result["prompt"]
    assert "干湿两用" not in result["prompt"]
    assert "水滴声和纸巾摩擦声" in result["prompt"]
    assert any("3 个镜头" in warning for warning in result["plan"]["warnings"])


def test_structured_video_sections_accept_timed_shot_labels():
    result = compile_video_prompt(
        "风格设定：真实家庭浴室。\n"
        "场景脚本：\n"
        "Shot 1（0.00-2.00s）：从包装底部抽出洗脸巾。\n"
        "Shot 2（2.00-5.00s）：微距展示压纹。\n"
        "技术约束：稳定镜头。",
        duration=5,
        model_id="seedance-2.0",
    )

    assert result["metadata"]["shot_count"] == 2
    assert result["plan"]["shots"] == [
        "从包装底部抽出洗脸巾",
        "微距展示压纹",
    ]


def test_structured_reverse_input_mode_does_not_use_direct_passthrough():
    text = "高端浴室广告。Shot 1：抽出洗脸巾。文字“干湿两用”浮现。"
    result = compile_video_prompt(
        {
            "input_mode": "structured_reverse",
            "user_instruction": text,
            "raw_text": text,
            "assembled_text": text,
            "final_text": text,
        },
        product_reference=True,
        fit_mode="single_clip",
    )

    assert result["prompt"].startswith("风格设定：")
    assert "Shot 1：抽出洗脸巾" in result["prompt"]
    assert "干湿两用" not in result["prompt"]
    assert result["plan"]["post_overlays"] == ["干湿两用"]
    assert result["metadata"].get("prompt_mode") != "direct_passthrough"


def test_structured_video_sections_keep_technical_constraints_out_of_shots():
    result = compile_video_prompt(
        (
            "风格设定：高端日系个护广告，真实家庭浴室，粉绿与米白色调，柔和自然光。\n"
            "场景脚本：\n"
            "Shot 1：0-5秒，手从悬挂包装底部抽出一张洗脸巾，稳定中近景。\n"
            "Shot 2：5-10秒，微距展开洗脸巾，展示3D如意云纹和厚度。\n"
            "技术约束：总时长10秒；画幅9:16；分辨率1080p；每镜一个主要动作；"
            "后期叠字：“干湿两用”；后期配音：“温柔开启一天”；音效：水滴声。"
        ),
        duration=10,
        model_id="doubao-seedance-2-0-mini-260615",
        provider="volcengine_ark",
    )

    assert result["plan"]["global_style"] == (
        "高端日系个护广告，真实家庭浴室，粉绿与米白色调，柔和自然光"
    )
    assert result["plan"]["shots"] == [
        "0-5秒，手从悬挂包装底部抽出一张洗脸巾，稳定中近景",
        "5-10秒，微距展开洗脸巾，展示3D如意云纹和厚度",
    ]
    assert "总时长10秒" in result["plan"]["technical_constraints"]
    assert "画幅9:16" in result["plan"]["technical_constraints"]
    assert all("技术约束" not in shot for shot in result["plan"]["shots"])
    assert result["plan"]["post_overlays"] == ["干湿两用"]
    assert result["plan"]["voiceover"] == "温柔开启一天"
    assert result["plan"]["sfx"] == ["水滴声"]
    assert result["prompt"].startswith("风格设定：")
    assert "\n场景脚本：\nShot 1：" in result["prompt"]
    assert "\n技术约束：" in result["prompt"]


def test_structured_video_prompt_splits_unnumbered_scene_clauses():
    result = compile_video_prompt(
        "风格设定：真实生活方式短片。\n"
        "场景脚本：人物走进浴室；人物拿起毛巾；人物走出浴室。\n"
        "技术约束：稳定跟拍。",
        duration=5,
        model_id="doubao-seedance-2-0-mini-260615",
    )

    assert result["plan"]["shots"] == [
        "人物走进浴室",
        "人物拿起毛巾",
        "人物走出浴室",
    ]
    assert result["plan"]["technical_constraints"] == "稳定跟拍"
    assert all("技术约束" not in shot for shot in result["plan"]["shots"])


def test_text_to_video_does_not_invent_a_reference_and_returns_a_plan():
    result = compile_video_prompt(
        "高端日系个护广告。女主走近洗手台，轻柔地用洗脸巾擦拭脸颊。",
        duration=10,
        model_id="doubao-seedance-2-0-mini-260615",
        provider="volcengine_ark",
    )

    assert "参考图" not in result["prompt"]
    assert "参考片复刻" not in result["prompt"]
    assert result["plan"]["global_style"] == "高端日系个护广告"
    assert result["plan"]["shots"] == ["女主走近洗手台", "轻柔地用洗脸巾擦拭脸颊"]
    assert result["compiler_version"]
    assert result["metadata"]["duration"] == 10


def test_structured_portrait_reverse_keeps_clothing_structure_and_coverage():
    result = compile_video_prompt(
        {
            "风格": "品牌 Lookbook",
            "服装结构": "利落西装剪裁，挺括肩线和自然垂坠面料",
            "服装覆盖": "长袖及腕，长裤及踝，整体覆盖完整",
            "时序分镜": "0-5s 成年模特向镜头走近",
        },
        duration=5,
        model_id="seedance-2.0",
    )

    assert "服装结构：利落西装剪裁" in result["prompt"]
    assert "服装覆盖：长袖及腕" in result["prompt"]


def test_reverse_structured_post_fields_survive_frontend_shaped_prompt_layers():
    final_text = "高端日系个护广告。Shot 1：抽出洗脸巾。Shot 2：微距展开如意云纹。"
    result = compile_video_prompt(
        {
            "风格": "高端日系个护广告",
            "时序分镜": "0-5s 抽出洗脸巾；5-10s 微距展开如意云纹",
            "字幕卖点": "干湿两用",
            "raw_text": final_text,
            "assembled_text": final_text,
            "final_text": final_text,
        },
        duration=10,
        model_id="seedance-2.0",
    )

    assert result["plan"]["shots"] == [
        "0-5s 抽出洗脸巾",
        "5-10s 微距展开如意云纹",
    ]
    assert result["plan"]["post_overlays"] == ["干湿两用"]
    assert "干湿两用" not in result["prompt"]


def test_optimized_video_text_does_not_erase_structured_post_production_fields():
    result = compile_video_prompt(
        {
            "字幕卖点": "干湿两用",
            "旁白": "让洗脸这件事成为温柔的开始",
            "音效": "水滴声",
            "raw_text": "Shot 1：抽出洗脸巾。字幕‘旧字幕’。",
            "optimized_text": "Shot 1：抽出洗脸巾。",
            "assembled_text": "Shot 1：抽出洗脸巾。",
            "final_text": "Shot 1：抽出洗脸巾。",
        },
        duration=5,
        model_id="seedance-2.0",
    )

    assert result["plan"]["post_overlays"] == ["干湿两用"]
    assert result["plan"]["voiceover"] == "让洗脸这件事成为温柔的开始"
    assert result["plan"]["sfx"] == ["水滴声"]
    assert "干湿两用" not in result["prompt"]
    assert "水滴声" not in result["prompt"]


def test_quoted_text_overlay_with_trailing_appearance_verb_moves_to_post():
    result = compile_video_prompt(
        "明亮浴室。Shot 1：微距展开云纹；文字“干湿两用”浮现；慢速推近。",
        duration=5,
        model_id="seedance-2.0",
    )

    assert result["plan"]["post_overlays"] == ["干湿两用"]
    assert "干湿两用" not in result["prompt"]
    assert "文字" not in result["prompt"]
    assert "浮现" not in result["prompt"]
    assert "慢速推近" in result["prompt"]


def test_quoted_text_overlay_after_comma_moves_to_post_without_losing_visual_action():
    result = compile_video_prompt(
        "明亮浴室，文字“新品上市”浮现，慢速推近。",
        duration=5,
        model_id="seedance-2.0",
    )

    assert result["plan"]["post_overlays"] == ["新品上市"]
    assert "新品上市" not in result["prompt"]
    assert "文字" not in result["prompt"]
    assert "浮现" not in result["prompt"]
    assert "明亮浴室" in result["prompt"]
    assert "慢速推近" in result["prompt"]


def test_product_reference_locks_only_product_identity_fields():
    result = compile_video_prompt(
        "高端个护广告。女主在浴室从墙面包装下方抽出一张洗脸巾。",
        product_reference=True,
        references=[{"role": "product", "url": "https://example.com/product.png"}],
    )

    lock = result["plan"]["subject_lock"]
    assert "包装" in lock
    assert "Logo" in lock
    assert "结构" in lock
    assert "纹理" in lock
    assert "人物" not in lock
    assert "场景" not in lock
    assert "光线" not in lock
    assert "构图" not in lock
    assert "女主在浴室" in result["prompt"]


def test_structured_reverse_prompt_preserves_generation_and_post_fields():
    raw = {
        "风格": "高端日系美妆个护",
        "光线": "粉绿与奶油色，偏冷柔光",
        "时序分镜": "0-4s 从包装下方抽出一张洗脸巾；4-8s 微距展开并展示云纹",
        "字幕卖点": "干湿两用；厚实吸水",
        "旁白": "让洗脸这件事，成为一天温柔的开始。",
        "音效": "水滴落水声；细微纸张摩擦声",
        "final_text": "不应用该字段覆盖结构化内容",
    }

    result = compile_video_prompt(raw, duration=8, model_id="seedance-2.0")
    plan = result["plan"]

    assert plan["global_style"] == "高端日系美妆个护；粉绿与奶油色，偏冷柔光"
    assert plan["shots"] == [
        "0-4s 从包装下方抽出一张洗脸巾",
        "4-8s 微距展开并展示云纹",
    ]
    assert plan["post_overlays"] == ["干湿两用", "厚实吸水"]
    assert plan["voiceover"] == "让洗脸这件事，成为一天温柔的开始。"
    assert plan["sfx"] == ["水滴落水声", "细微纸张摩擦声"]
    assert result["metadata"]["post_overlays"] == ["干湿两用", "厚实吸水"]
    assert result["metadata"]["voiceover"].startswith("让洗脸这件事")
    assert result["metadata"]["sfx"] == ["水滴落水声", "细微纸张摩擦声"]
    assert "干湿两用" not in result["prompt"]
    assert "让洗脸这件事" not in result["prompt"]


def test_manual_video_prompt_replaces_stale_structured_shots_and_post_fields():
    result = compile_video_prompt(
        {
            "风格": "高端日系个护广告",
            "shots": ["旧镜头：包装静置在桌面"],
            "字幕卖点": "旧字幕",
            "旁白": "旧旁白",
            "user_instruction": (
                "保留柔和浴室光影。Shot 1：手从包装底部抽出洗脸巾；"
                "Shot 2：微距展开云纹。后期叠字：干湿两用；"
                "后期配音：“温柔开始每一天。”"
            ),
            "final_text": "与 user_instruction 相同的组装结果不应被重复注入",
        },
        duration=10,
        model_id="seedance-2.0",
    )

    assert result["plan"]["shots"] == [
        "手从包装底部抽出洗脸巾",
        "微距展开云纹",
    ]
    assert result["plan"]["post_overlays"] == ["干湿两用"]
    assert result["plan"]["voiceover"] == "温柔开始每一天。"
    assert "旧镜头" not in result["prompt"]
    assert "旧字幕" not in result["metadata"]["post_overlays"]
    assert "高端日系个护广告" not in result["prompt"]
    assert "保留柔和浴室光影" in result["prompt"]


def test_layered_assembled_prompt_replaces_stale_structured_shots_without_user_instruction():
    result = compile_video_prompt(
        {
            "shots": ["旧分镜：产品静置"],
            "raw_text": "原始稿：产品静置",
            "optimized_text": "优化稿：产品缓慢入镜",
            "assembled_text": "Shot 1：手持产品稳定入镜；Shot 2：慢速推近 Logo 特写。",
            "final_text": "Shot 1：手持产品稳定入镜；Shot 2：慢速推近 Logo 特写。",
        },
        duration=10,
        model_id="seedance-2.0",
    )

    assert result["plan"]["shots"] == [
        "手持产品稳定入镜",
        "慢速推近 Logo 特写",
    ]
    assert "旧分镜" not in result["prompt"]


def test_structured_reverse_final_text_replaces_stale_full_timeline():
    final_text = (
        "兰蔻小黑瓶精华液，黑色渐变瓶身和银色玫瑰浮雕瓶盖；"
        "按原镜头顺序压缩为单段核心版；"
        "镜头1：黑底品牌标志切到透明滴管特写；"
        "镜头2：银色瓶盖与带水滴的瓶身特写；"
        "镜头3：完整产品立于浅蓝色水面并以涟漪收尾"
    )
    result = compile_video_prompt(
        {
            "图像类型": "产品视频",
            "主体": "兰蔻小黑瓶精华液，黑色渐变瓶身和银色玫瑰浮雕瓶盖",
            "时序分镜": (
                "0-7s 黑底品牌标志；8-10s 银色瓶盖；11-13s 水滴飞溅；"
                "14-16s 瓶身局部；17-19s 水波空镜；21-23s 完整产品；"
                "24-25s 纯黑画面"
            ),
            "旁白": "未分析",
            "音效": "未分析",
            "input_mode": "structured_reverse",
            "raw_text": final_text,
            "assembled_text": final_text,
            "final_text": final_text,
        },
        duration=10,
        model_id="doubao-seedance-1-5-pro-251215",
        provider="volcengine_ark",
        references=[{"role": "motion_analysis"}],
        fit_mode="single_clip",
    )

    assert result["plan"]["shots"] == [
        "黑底品牌标志切到透明滴管特写",
        "银色瓶盖与带水滴的瓶身特写",
        "完整产品立于浅蓝色水面并以涟漪收尾",
    ]
    assert result["metadata"]["source_shot_count"] == 3
    assert "水波空镜" not in result["prompt"]
    assert "纯黑画面" not in result["prompt"]
    assert "主体锁定：全程保持同一产品主体" in result["prompt"]
    assert result["plan"]["voiceover"] == ""
    assert result["plan"]["sfx"] == []


def test_structured_product_video_without_timeline_keeps_scene_action_and_camera():
    result = compile_video_prompt(
        {
            "场景背景": "暖棕色广告棚景和金色沙粒台面",
            "风格": "高端美妆广告",
            "视角构图": "竖屏 9:16，产品居中，浅景深",
            "主体动作": "参考商品从画面左侧入场，缓慢旋转，水花飞溅后切到 Logo 特写",
            "镜头运动": "缓慢推进并轻微环绕",
            "光线": "柔和电影感主光和边缘高光",
            "一致性约束": "产品包装结构、Logo 和正面可见文字保持稳定",
            "final_text": "产品旋转展示，水花飞溅，镜头推进",
        },
        duration=5,
        product_reference=True,
    )

    assert result["plan"]["shots"] == ["参考商品从画面左侧入场，缓慢旋转，水花飞溅后切到 Logo 特写"]
    assert "暖棕色广告棚景和金色沙粒台面" in result["prompt"]
    assert "缓慢推进并轻微环绕" in result["prompt"]
    assert "视角构图：竖屏 9:16，产品居中，浅景深" in result["prompt"]
    assert "一致性约束：产品包装结构、Logo 和正面可见文字保持稳定" in result["prompt"]
    assert "参考商品从画面左侧入场" in result["prompt"]


def test_structured_general_video_keeps_subject_body_composition_and_consistency():
    result = compile_video_prompt(
        {
            "主体": "成年女性模特",
            "人物比例": "7.5 头身，自然身体比例",
            "身材体态": "自然肩颈线条和放松站姿",
            "景别": "中景转面部近景",
            "视角构图": "竖屏 9:16，人物居中",
            "主体动作": "女主轻柔擦拭脸颊",
            "一致性约束": "全程保持同一成年人的脸型、发型和服装",
        },
        duration=5,
    )

    assert "主体：成年女性模特" in result["prompt"]
    assert "人物比例：7.5 头身，自然身体比例" in result["prompt"]
    assert "身材体态：自然肩颈线条和放松站姿" in result["prompt"]
    assert "景别：中景转面部近景" in result["prompt"]
    assert "一致性约束：全程保持同一成年人的脸型、发型和服装" in result["prompt"]


def test_seedance_mini_overload_requires_a_sequence_without_losing_shots():
    raw = {
        "风格": "高端日系个护广告",
        "shots": [
            "女主走近墙面包装并从下方抽出一张洗脸巾",
            "微距展开洗脸巾，展示3D如意云纹和厚度",
            "俯拍浸水并拧干，展示吸水性",
            "女主轻拭脸颊，镜头拉远至洗手台中景",
        ],
    }

    result = compile_video_prompt(
        raw,
        duration=10,
        model_id="doubao-seedance-2-0-mini-260615",
        provider="volcengine_ark",
    )

    assert result["profile"]["family"] == "seedance_mini"
    assert result["profile"]["recommended_max_shots"] == 2
    assert result["sequence_required"] is True
    assert result["metadata"]["shot_count"] == 4
    assert result["metadata"]["omitted_shot_count"] == 0
    assert result["metadata"]["recommended_clip_count"] == 2
    assert any("建议最多 2 个镜头" in warning for warning in result["plan"]["warnings"])
    for number, shot in enumerate(raw["shots"], start=1):
        assert f"Shot {number}：{shot}" in result["prompt"]


def test_single_clip_fit_preserves_long_washcloth_script_and_warns_about_density():
    raw = {
        "风格": "高端日系美妆个护广告，真实家庭浴室，粉绿、米白和奶油色调，柔和自然光",
        "技术约束": (
            "产品外包装长20cm、宽10cm、高24cm；洗脸巾为20cm见方；"
            "包装图案和3D如意云纹与参考图保持相同"
        ),
        "shots": [
            "女主舒缓伸懒腰后走向墙面悬挂包装",
            "手从包装底部轻轻抽出一张洗脸巾",
            "微距拍摄双手缓慢展开洗脸巾，展示3D如意云压纹和厚度",
            "俯拍洗脸巾浸水并形成扩散波纹",
            "双手拧干湿洗脸巾，水流飞溅",
            "女主用洗脸巾轻拭脸颊",
            "洗脸巾从手背擦拭到手臂，展示背面压纹",
            "双手撕开洗脸巾，展示断面纤维",
            "包装置于白色圆桌中央形成产品收尾镜头",
        ],
        "字幕": ["干湿两用", "厚实吸水", "亲肤"],
        "旁白": "让洗脸这件事，成为一天温柔的开始",
    }

    result = compile_video_prompt(
        raw,
        duration=10,
        model_id="doubao-seedance-2-0-mini-260615",
        provider="volcengine_ark",
        product_reference=True,
        fit_mode="single_clip",
    )

    metadata = result["metadata"]
    assert result["sequence_required"] is False
    assert result["profile"]["recommended_max_shots"] == 2
    assert metadata["source_shot_count"] == 9
    assert metadata["selected_shot_count"] == 9
    assert metadata["omitted_shot_count"] == 0
    assert metadata["condensed_for_single_clip"] is False
    assert "底部轻轻抽出" in result["prompt"]
    assert "3D如意云压纹" in result["prompt"]
    assert "产品身份约束" in result["prompt"]
    assert "浸水" in result["prompt"]
    assert "拧干" in result["prompt"]
    assert "撕开" in result["prompt"]
    assert "包装置于白色圆桌中央" in result["prompt"]
    assert "干湿两用" not in result["prompt"]
    assert "让洗脸这件事" not in result["prompt"]
    assert any("动作密度较高" in warning for warning in result["plan"]["warnings"])
    assert metadata["prompt_char_count"] <= result["profile"]["prompt_budget_chars"]


def test_single_clip_fit_parses_unstructured_washcloth_brief_without_counting_style_as_shots():
    raw = (
        "高端日系美妆个护广告；画面以粉绿/米白/奶油色系主调；"
        "冷暖平衡，偏冷但不刺眼；慢动作+轻微推拉；无明显晃动，稳定器拍摄感；"
        "场景设置为真实家庭浴室，非影棚布景。"
        "产品外包装尺寸：长20cm、宽10cm、高24cm，洗脸巾20cm见方，"
        "纹路为3D如意云纹，与参考图保持相同，视频画面要求："
        "女主走到悬挂产品处，手从包装底部抽出洗脸巾，"
        "微距展开并展示3D如意云压纹和厚度；俯拍浸水；双手拧干；"
        "擦拭脸颊；撕开展示纤维；"
        "旁白：“让洗脸这件事，成为一天温柔的开始"
    )

    result = compile_video_prompt(
        raw,
        duration=10,
        model_id="doubao-seedance-2-0-mini-260615",
        provider="volcengine_ark",
        product_reference=True,
        fit_mode="single_clip",
    )

    assert result["sequence_required"] is False
    assert result["metadata"]["selected_shot_count"] == result["metadata"]["source_shot_count"]
    assert result["metadata"]["omitted_shot_count"] == 0
    assert "粉绿/米白/奶油色系主调" in result["plan"]["global_style"]
    assert "稳定器拍摄感" in result["plan"]["global_style"]
    assert "产品外包装尺寸：长20cm" in result["plan"]["technical_constraints"]
    assert "洗脸巾20cm见方" in result["plan"]["technical_constraints"]
    assert "底部抽出洗脸巾" in result["prompt"]
    assert "3D如意云压纹" in result["prompt"]
    assert "俯拍浸水" in result["prompt"]
    assert "双手拧干" in result["prompt"]
    assert "擦拭脸颊" in result["prompt"]
    assert "撕开展示纤维" in result["prompt"]
    assert result["plan"]["voiceover"] == "让洗脸这件事，成为一天温柔的开始"


def test_single_clip_budget_compaction_never_truncates_user_actions():
    result = compile_video_prompt(
        {
            "风格": "高端日系自然生活方式广告" * 20,
            "shots": ["手从包装底部抽出洗脸巾", "微距展开3D如意云纹"],
            "技术约束": "保持同一SKU包装、Logo、文字、尺寸和纹理" * 30,
        },
        duration=10,
        model_id="custom-video",
        product_reference=True,
        fit_mode="single_clip",
        extra={"video_prompt_profile": {"max_prompt_chars": 240}},
    )

    assert result["sequence_required"] is False
    assert result["metadata"]["prompt_char_count"] > 240
    assert result["metadata"]["prompt_over_budget"] is True
    assert result["metadata"]["compacted_for_budget"] is True
    assert result["metadata"]["recommended_clip_count"] == 1
    assert result["plan"]["shots"] == [
        "手从包装底部抽出洗脸巾",
        "微距展开3D如意云纹",
    ]
    assert "手从包装底部抽出洗脸巾" in result["prompt"]
    assert "微距展开3D如意云纹" in result["prompt"]
    assert "保持同一SKU包装、Logo、文字、尺寸和纹理" in result["prompt"]
    assert "拆分为多段" not in " ".join(result["plan"]["warnings"])


def test_single_clip_reports_over_budget_instead_of_cutting_a_long_action():
    action = (
        "女主走到墙面悬挂包装前，从包装底部完整抽出一张洗脸巾，"
        "双手缓慢展开并对折展示厚度，随后浸水、拧干、擦拭脸颊，"
        "最后撕开展示断面纤维并让包装与洗脸巾同框收尾"
    )

    result = compile_video_prompt(
        {"shots": [action]},
        duration=10,
        model_id="custom-video",
        fit_mode="single_clip",
        extra={"video_prompt_profile": {"max_prompt_chars": 80}},
    )

    assert result["sequence_required"] is False
    assert result["plan"]["shots"] == [action]
    assert action in result["prompt"]
    assert result["metadata"]["prompt_char_count"] > 80
    assert result["metadata"]["prompt_over_budget"] is True
    assert result["metadata"]["recommended_clip_count"] == 1


def test_single_clip_over_budget_keeps_product_identity_and_structured_sections():
    custom_constraint = "不得新增用户未提供的商品、配件或突兀道具"
    result = compile_video_prompt(
        {
            "风格": "高端日系个护广告，柔和浴室自然光",
            "shots": ["女主从悬挂包装底部抽出洗脸巾并展开3D如意云纹"],
            "技术约束": (
                "保持同一SKU包装、Moon Logo、可见文字、底部出纸口和纹理；"
                f"{custom_constraint}"
            ),
        },
        duration=10,
        model_id="custom-video",
        product_reference=True,
        product_lock_mode="locked",
        fit_mode="single_clip",
        extra={"video_prompt_profile": {"max_prompt_chars": 80}},
    )

    assert result["sequence_required"] is False
    assert result["metadata"]["prompt_over_budget"] is True
    assert "风格设定：" in result["prompt"]
    assert "场景脚本：" in result["prompt"]
    assert "技术约束：" in result["prompt"]
    assert "同一SKU" in result["prompt"]
    assert "Moon Logo" in result["prompt"]
    assert "可见文字" in result["prompt"]
    assert custom_constraint in result["prompt"]


def test_single_clip_does_not_reclassify_unknown_product_action_as_constraint():
    action = "洗脸巾纹路在水面上呈现由中心向外扩散的波纹"
    result = compile_video_prompt(
        {"shots": [action]},
        duration=10,
        product_reference=True,
        fit_mode="single_clip",
    )

    assert result["plan"]["shots"] == [action]
    assert f"Shot 1：{action}" in result["prompt"]


def test_single_clip_keeps_unknown_product_action_order_when_it_mentions_reference_lock():
    first_action = "包装图与参考图保持相同，双手托举产品"
    second_action = "双手拧干洗脸巾"

    result = compile_video_prompt(
        {"shots": [first_action, second_action]},
        duration=10,
        product_reference=True,
        fit_mode="single_clip",
    )

    assert result["plan"]["shots"] == [first_action, second_action]
    assert f"Shot 1：{first_action}" in result["prompt"]
    assert f"Shot 2：{second_action}" in result["prompt"]


def test_single_clip_always_renders_the_three_section_contract():
    result = compile_video_prompt(
        {"shots": ["产品稳定入镜"]},
        duration=10,
        fit_mode="single_clip",
    )

    assert result["prompt"].startswith("风格设定：")
    assert "\n场景脚本：\n" in result["prompt"]
    assert "\n技术约束：" in result["prompt"]


def test_structured_shots_move_post_production_out_of_generation_prompt():
    result = compile_video_prompt(
        {
            "风格设定": "高端日系美妆个护广告，真实家庭浴室",
            "shots": [
                "手从包装底部抽出洗脸巾；字幕：“干湿两用”",
                "双手展开洗脸巾展示3D如意云纹；旁白：“温柔开始”；音效：水滴声",
            ],
            "技术约束": "保持同一SKU包装与Logo",
        },
        duration=10,
        product_reference=True,
        fit_mode="single_clip",
    )

    assert "底部抽出洗脸巾" in result["prompt"]
    assert "3D如意云纹" in result["prompt"]
    assert "干湿两用" not in result["prompt"]
    assert "温柔开始" not in result["prompt"]
    assert "水滴声" not in result["prompt"]
    assert result["plan"]["post_overlays"] == ["干湿两用"]
    assert result["plan"]["voiceover"] == "温柔开始"
    assert result["plan"]["sfx"] == ["水滴声"]


def test_single_clip_product_action_uses_prompt_driven_identity_guard():
    result = compile_video_prompt(
        "手从包装底部抽出洗脸巾；微距展开3D如意云纹",
        duration=10,
        product_reference=True,
        product_lock_mode="locked",
        fit_mode="single_clip",
    )

    assert "底部抽出洗脸巾" in result["prompt"]
    assert "微距展开3D如意云纹" in result["prompt"]
    assert "产品视频策略：提示词驱动" in result["prompt"]
    assert "不得据此删除、替换或降速用户动作" in result["prompt"]
    assert "避免快速旋转" not in result["prompt"]


def test_single_clip_preserves_product_actions_after_interaction_and_closing():
    result = compile_video_prompt(
        {
            "shots": [
                "从包装底部抽出并展开洗脸巾纹理",
                "包装与洗脸巾同框收尾",
                "俯拍洗脸巾浸水",
            ]
        },
        duration=10,
        model_id="doubao-seedance-2-0-mini-260615",
        product_reference=True,
        fit_mode="single_clip",
    )

    assert result["plan"]["shots"] == [
        "从包装底部抽出并展开洗脸巾纹理",
        "包装与洗脸巾同框收尾",
        "俯拍洗脸巾浸水",
    ]


def test_single_clip_general_story_preserves_all_actions_in_order():
    result = compile_video_prompt(
        "运动员冲刺起跑；连续跨栏；冲过终点；庆祝后拿起水杯",
        duration=5,
        model_id="doubao-seedance-2-0-mini-260615",
        fit_mode="single_clip",
    )

    assert result["plan"]["shots"] == [
        "运动员冲刺起跑",
        "连续跨栏",
        "冲过终点",
        "庆祝后拿起水杯",
    ]


def test_unlabelled_actions_and_repeated_beats_are_all_counted_as_shots():
    result = compile_video_prompt(
        "女主伸懒腰；走到墙面包装；女主伸懒腰",
        duration=10,
        model_id="doubao-seedance-2-0-mini-260615",
        provider="volcengine_ark",
    )

    assert result["plan"]["global_style"] == ""
    assert result["plan"]["shots"] == ["女主伸懒腰", "走到墙面包装", "女主伸懒腰"]
    assert result["metadata"]["shot_count"] == 3
    assert result["sequence_required"] is True


def test_comma_separated_actions_are_counted_for_capacity_without_omission():
    result = compile_video_prompt(
        "走近包装，抽出洗脸巾，展开，浸水，拧干，擦脸",
        duration=10,
        model_id="doubao-seedance-2-0-mini-260615",
        provider="volcengine_ark",
    )

    assert result["plan"]["shots"] == [
        "走近包装",
        "抽出洗脸巾",
        "展开",
        "浸水",
        "拧干",
        "擦脸",
    ]
    assert result["metadata"]["shot_count"] == 6
    assert result["metadata"]["omitted_shot_count"] == 0
    assert result["sequence_required"] is True


def test_two_comma_separated_actions_respect_single_shot_capacity():
    result = compile_video_prompt(
        "走近包装，抽出洗脸巾",
        duration=5,
        model_id="doubao-seedance-2-0-mini-260615",
        provider="volcengine_ark",
    )

    assert result["plan"]["shots"] == ["走近包装", "抽出洗脸巾"]
    assert result["metadata"]["shot_count"] == 2
    assert result["profile"]["recommended_max_shots"] == 1
    assert result["sequence_required"] is True


def test_scene_context_with_a_subject_action_is_not_misclassified_as_style():
    result = compile_video_prompt(
        "女主在家庭浴室洗脸",
        duration=5,
        model_id="doubao-seedance-2-0-mini-260615",
    )

    assert result["plan"]["global_style"] == ""
    assert result["plan"]["shots"] == ["女主在家庭浴室洗脸"]


def test_subject_in_a_styled_scene_still_counts_as_a_shot():
    result = compile_video_prompt(
        "女主在家庭浴室，柔和光线",
        duration=5,
        model_id="doubao-seedance-2-0-mini-260615",
    )

    assert result["plan"]["global_style"] == ""
    assert result["plan"]["shots"] == ["女主在家庭浴室，柔和光线"]


def test_unlisted_subjects_in_a_scene_are_not_swallowed_as_global_context():
    for prompt in ("成年女性在家庭浴室护肤", "用户在卫生间清洁面部"):
        result = compile_video_prompt(
            prompt,
            duration=5,
            model_id="doubao-seedance-2-0-mini-260615",
        )

        assert result["plan"]["global_style"] == ""
        assert result["plan"]["shots"] == [prompt]


def test_scene_context_subjects_do_not_count_as_video_shots():
    for prompt in ("视频发生在家庭浴室，柔和光线", "本片发生在浴室", "画面位于卧室"):
        result = compile_video_prompt(
            prompt,
            duration=5,
            model_id="doubao-seedance-2-0-mini-260615",
        )

        assert result["plan"]["global_style"] == prompt
        assert result["plan"]["shots"] == []


def test_product_lock_mode_and_template_change_the_compiled_strategy():
    raw = {"final_text": "Shot 1：产品在浴室台面稳定展示。"}
    locked = compile_video_prompt(
        raw,
        product_reference=True,
        product_lock_mode="locked",
        product_video_template="stable_showcase",
    )
    free = compile_video_prompt(
        raw,
        product_reference=True,
        product_lock_mode="free",
        product_video_template="reference_sequence",
    )

    assert "文字保真模式" in locked["prompt"]
    assert "稳定陈列" in locked["prompt"]
    assert "参考分镜" in free["prompt"]
    assert "不强制每个镜头静态正面" in free["prompt"]
    assert locked["metadata"]["product_lock_mode"] == "locked"
    assert free["metadata"]["product_video_template"] == "reference_sequence"


def test_prompt_driven_product_strategy_preserves_explicit_motion():
    result = compile_video_prompt(
        "产品缓慢旋转一周，水花飞溅，随后快速推近包装 Logo 特写。",
        duration=10,
        product_reference=True,
        product_lock_mode="locked",
        product_video_template="prompt_driven",
    )

    assert "产品缓慢旋转一周" in result["prompt"]
    assert "水花飞溅" in result["prompt"]
    assert "快速推近包装 Logo 特写" in result["prompt"]
    assert "产品身份保真" in result["prompt"]
    assert "提示词驱动" in result["prompt"]
    assert "稳定陈列" not in result["prompt"]
    assert "避免快速旋转" not in result["prompt"]
    assert result["metadata"]["product_video_template"] == "prompt_driven"


def test_style_motion_and_frame_references_compile_role_boundaries():
    result = compile_video_prompt(
        "Shot 1：产品缓慢入镜。",
        references=[
            {"role": "style", "source": "style_reference_image"},
            {"role": "motion", "source": "source_asset_url"},
            {"role": "first_frame", "source": "first_frame_image"},
            {"role": "last_frame", "source": "last_frame_image"},
        ],
    )

    assert "风格参考仅迁移" in result["prompt"]
    assert "动作参考仅迁移" in result["prompt"]
    assert "首帧定义开始状态" in result["prompt"]
    assert "尾帧定义结束状态" in result["prompt"]
    assert len(result["plan"]["reference_guidance"]) == 3


def test_uploaded_video_is_analysis_only_without_native_video_transport():
    references = build_video_prompt_references(
        source_asset_url="/api/uploads/reference.mp4",
        source_type="video",
        params={},
    )
    result = compile_video_prompt(
        "Shot 1：模仿反推得到的镜头节奏展示产品。",
        references=references,
    )

    assert references == [
        {
            "role": "motion_analysis",
            "source": "source_asset_url",
            "mode": "analysis_only",
        }
    ]
    assert "动作参考仅迁移" not in result["prompt"]
    assert "镜头顺序与节奏以原视频抽帧反推后的当前场景脚本为准" in result["prompt"]
    assert "不迁移其中的人物、商品、品牌" not in result["prompt"]
    assert result["metadata"]["reference_roles"] == ["motion_analysis"]
    assert result["metadata"]["motion_reference_mode"] == "analysis_only"


def test_manual_product_reverse_prompt_keeps_subject_identity_lock():
    text = (
        "兰蔻小黑瓶精华液，黑色渐变瓶身和银色玫瑰浮雕瓶盖；"
        "镜头1：瓶盖微距特写；镜头2：完整产品立于水面。"
    )
    result = compile_video_prompt(
        {
            "图像类型": "产品视频",
            "主体": "兰蔻小黑瓶精华液，黑色渐变瓶身和银色玫瑰浮雕瓶盖",
            "input_mode": "structured_reverse",
            "assembled_text": text,
            "final_text": text,
        },
        duration=10,
        model_id="doubao-seedance-1-5-pro-251215",
    )

    assert "主体锁定：全程保持同一产品主体" in result["prompt"]
    assert "黑色渐变瓶身" in result["plan"]["subject_lock"]
    assert "包装结构、比例、主色和材质连续一致" in result["plan"]["subject_lock"]


def test_analysis_only_source_video_is_not_submitted_as_first_frame(monkeypatch):
    task = GenTask(
        category="video",
        stage="final",
        source_type="video",
        source_asset_url="/api/uploads/reference.mp4",
        params={
            "duration": 10,
            "resolution": "720p",
            "ratio": "16:9",
            "_video_reference_roles": [
                {
                    "role": "motion_analysis",
                    "source": "source_asset_url",
                    "mode": "analysis_only",
                }
            ],
        },
    )
    monkeypatch.setattr(
        generation_video_submit,
        "gateway_video_first_frame",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("analysis-only source video must not become a first-frame reference")
        ),
    )

    params = generation_video_submit.video_submit_params(None, task)

    assert "first_frame_image" not in params
    assert "reference_image_url" not in params


def test_product_detail_references_keep_ordered_numbered_roles():
    references = build_video_prompt_references(
        source_asset_url="/api/uploads/product.png",
        source_type="image",
        params={
            "subject_mode": "product",
            "product_reference_image": "/api/uploads/product.png",
            "product_detail_images": [
                "/api/uploads/detail-a.png",
                "/api/uploads/detail-b.png",
                "/api/uploads/detail-c.png",
                "/api/uploads/detail-d.png",
                "/api/uploads/detail-e.png",
            ],
        },
    )

    assert references == [
        {"role": "product", "source": "source_asset_url"},
        {"role": "product_detail_1", "source": "product_detail_images[0]"},
        {"role": "product_detail_2", "source": "product_detail_images[1]"},
        {"role": "product_detail_3", "source": "product_detail_images[2]"},
        {"role": "product_detail_4", "source": "product_detail_images[3]"},
        {"role": "product_detail_5", "source": "product_detail_images[4]"},
    ]


def test_model_profiles_distinguish_grok_and_allow_runtime_override():
    grok = infer_video_model_profile(
        model_id="grok-imagine-video-1.5",
        provider="yinyue",
        duration=10,
    )
    overridden = infer_video_model_profile(
        model_id="custom-video-v7",
        provider="private",
        duration=10,
        extra={"video_prompt_profile": {"family": "private_v7", "recommended_max_shots": 5}},
    )

    assert grok["family"] == "grok"
    assert grok["recommended_max_shots"] == 3
    assert overridden["family"] == "private_v7"
    assert overridden["recommended_max_shots"] == 5


def test_legacy_prompt_profile_resolves_duration_capacity():
    profile = infer_video_model_profile(
        model_id="grok-imagine-video-1.5",
        duration=8,
        extra={
            "prompt_profile": {
                "max_prompt_chars": 900,
                "max_shots_by_duration": {"5": 2, "10": 3, "15": 4},
            }
        },
    )

    assert profile["recommended_max_shots"] == 3
    assert profile["prompt_budget_chars"] == 900


def test_invalid_runtime_prompt_profile_falls_back_without_crashing():
    result = compile_video_prompt(
        "Shot 1：产品稳定入镜。",
        duration=10,
        model_id="doubao-seedance-2-0-mini-260615",
        extra={
            "video_prompt_profile": {
                "recommended_max_shots": "many",
                "max_shots_by_duration": {"five": "many", "10": "two"},
                "seconds_per_shot": "five",
                "max_shots_cap": "many",
                "max_prompt_chars": "lots",
            }
        },
    )

    assert result["profile"]["recommended_max_shots"] == 2
    assert result["profile"]["prompt_budget_chars"] == 1200
    assert result["sequence_required"] is False

    nonpositive = infer_video_model_profile(
        model_id="doubao-seedance-2-0-mini-260615",
        duration=10,
        extra={"prompt_profile": {"max_prompt_chars": -50}},
    )
    assert nonpositive["prompt_budget_chars"] == 1200


def test_prompt_budget_overload_requires_sequence_without_truncating_content():
    long_action = "女主稳定抽出洗脸巾并展开展示如意云纹" * 8
    result = compile_video_prompt(
        {
            "风格": "高端日系个护广告",
            "shots": [long_action],
        },
        duration=10,
        model_id="custom-video",
        extra={"video_prompt_profile": {"prompt_budget_chars": 80}},
    )

    assert result["metadata"]["shot_count"] == 1
    assert result["metadata"]["prompt_char_count"] > 80
    assert result["sequence_required"] is True
    assert result["metadata"]["recommended_clip_count"] >= 2
    assert long_action in result["prompt"]
    assert any("提示词预算" in warning for warning in result["plan"]["warnings"])


def test_final_text_extracts_post_production_before_counting_shots():
    raw_text = (
        "真实家庭浴室，米白和浅粉绿色调。Shot 1：手从墙面包装底部抽出一张洗脸巾；"
        "Shot 2：微距展开并展示3D如意云纹。字幕“干湿两用”；"
        "旁白：“让洗脸这件事，成为一天温柔的开始。”"
    )

    result = compile_video_prompt(
        {"final_text": raw_text},
        duration=10,
        model_id="doubao-seedance-2-0-mini-260615",
    )

    assert result["sequence_required"] is False
    assert result["plan"]["shots"] == [
        "手从墙面包装底部抽出一张洗脸巾",
        "微距展开并展示3D如意云纹",
    ]
    assert result["plan"]["post_overlays"] == ["干湿两用"]
    assert result["plan"]["voiceover"] == "让洗脸这件事，成为一天温柔的开始。"
    assert "干湿两用" not in result["prompt"]
    assert "让洗脸这件事" not in result["prompt"]


def test_optimizer_post_production_labels_do_not_become_video_shots():
    raw_text = (
        "高端日系个护广告。Shot 1：从包装底部抽出洗脸巾；"
        "后期叠字：干湿两用、厚实吸水；"
        "后期配音：“让洗脸这件事，成为一天温柔的开始。”"
    )

    result = compile_video_prompt(raw_text, duration=5, model_id="seedance-2.0")

    assert result["plan"]["shots"] == ["从包装底部抽出洗脸巾"]
    assert result["plan"]["post_overlays"] == ["干湿两用、厚实吸水"]
    assert result["plan"]["voiceover"] == "让洗脸这件事，成为一天温柔的开始。"
    assert "后期叠字" not in result["prompt"]
    assert "后期配音" not in result["prompt"]


def test_post_production_labels_without_colons_do_not_leak_into_generation_prompt():
    raw_text = "Shot 1：从包装底部抽出洗脸巾；" "字幕“干湿两用”；旁白“温柔开启新一天”；SFX“水滴声”"

    result = compile_video_prompt(raw_text, duration=5, model_id="seedance-2.0")

    assert result["plan"]["shots"] == ["从包装底部抽出洗脸巾"]
    assert result["plan"]["post_overlays"] == ["干湿两用"]
    assert result["plan"]["voiceover"] == "温柔开启新一天"
    assert result["plan"]["sfx"] == ["水滴声"]
    assert "干湿两用" not in result["prompt"]
    assert "温柔开启新一天" not in result["prompt"]
    assert "水滴声" not in result["prompt"]


def test_unquoted_post_production_labels_without_colons_are_extracted():
    raw_text = "Shot 1：从包装底部抽出洗脸巾；" "字幕干湿两用；旁白温柔开启新一天；SFX水滴声"

    result = compile_video_prompt(raw_text, duration=5, model_id="seedance-2.0")

    assert result["plan"]["shots"] == ["从包装底部抽出洗脸巾"]
    assert result["plan"]["post_overlays"] == ["干湿两用"]
    assert result["plan"]["voiceover"] == "温柔开启新一天"
    assert result["plan"]["sfx"] == ["水滴声"]
    assert "干湿两用" not in result["prompt"]
    assert "温柔开启新一天" not in result["prompt"]
    assert "水滴声" not in result["prompt"]


def test_plain_product_text_description_is_not_treated_as_an_overlay_label():
    prompts = (
        "产品包装文字保持清晰，镜头推近 Logo",
        "产品包装文字“PURE”保持清晰",
        "画面无字幕无旁白",
        "不要字幕，包装文字清晰",
    )

    for prompt in prompts:
        result = compile_video_prompt(prompt, duration=5, model_id="seedance-2.0")

        assert result["plan"]["post_overlays"] == []
        assert result["plan"]["voiceover"] == ""
        assert result["plan"]["shots"] == [prompt]
        assert prompt in result["prompt"]


def test_product_identity_profile_is_kept_in_one_product_guard():
    result = compile_video_prompt(
        {
            "产品身份档案": "同一SKU悬挂式洗脸巾包装，保持Moon Logo、底部出纸口和3D如意云纹。",
            "final_text": "真实家庭浴室。Shot 1：手从包装底部抽出一张洗脸巾。",
        },
        product_reference=True,
    )

    assert result["prompt"].count("产品身份约束") == 1
    assert "Moon Logo" in result["plan"]["subject_lock"]
    assert "底部出纸口" in result["plan"]["subject_lock"]
    assert "3D如意云纹" in result["plan"]["subject_lock"]
    assert "场景" not in result["plan"]["subject_lock"]
    assert "光线" not in result["plan"]["subject_lock"]


def test_character_reference_adds_one_portrait_identity_guard_with_profile_facts():
    result = compile_video_prompt(
        {
            "人物身份档案": "成年女性，齐肩黑发，椭圆脸，左眼下有泪痣。",
            "final_text": "柔和商业人像。Shot 1：女主走向洗手台。",
        },
        references=[{"role": "character", "url": "https://example.com/person.png"}],
    )

    assert result["prompt"].count("人物身份约束") == 1
    assert "齐肩黑发" in result["plan"]["subject_lock"]
    assert "左眼下有泪痣" in result["plan"]["subject_lock"]
    assert "场景" not in result["plan"]["subject_lock"]
    assert result["metadata"]["reference_roles"] == ["character"]


def test_compiler_deduplicates_guard_and_style_but_preserves_repeated_timeline_beats():
    result = compile_video_prompt(
        {
            "风格": "产品视频模板：参考分镜；产品视频模板：参考分镜",
            "产品身份档案": "产品身份约束：同一SKU绿色悬挂包装；同一SKU绿色悬挂包装",
            "shots": [
                "0-4s 从底部抽出一张洗脸巾",
                "0-4s 从底部抽出一张洗脸巾",
                "4-8s 微距展开如意云纹",
            ],
        },
        product_reference=True,
        duration=8,
        model_id="seedance-2.0",
    )

    assert result["prompt"].count("产品身份约束") == 1
    assert result["prompt"].count("产品视频模板：参考分镜") == 1
    assert result["prompt"].count("从底部抽出一张洗脸巾") == 2
    assert result["plan"]["shots"] == [
        "0-4s 从底部抽出一张洗脸巾",
        "0-4s 从底部抽出一张洗脸巾",
        "4-8s 微距展开如意云纹",
    ]


def test_long_direct_seedance_prompt_compacts_runtime_metadata_and_duplicate_inventory():
    shots = [
        f"Shot {index}：同一女性与同一洗脸巾包装完成动作{index}，"
        "保持包装正面文字、珍珠纹理、暖色侧光和固定机位；硬切。"
        + ("画面文字：干湿两用。" if index == 5 else "")
        + ("声音：持续背景音乐。" if index in (1, 5, 10) else "")
        for index in range(1, 11)
    ]
    inventory = "；".join(
        f"{index}. 画面执行第{index}个已在场景脚本详细描述的动作，"
        "主体追踪、姿态、运镜、光线和转场再次完整重复，"
        "再次逐项复述镜头内的主体位置、动作初态终态、景别、光线方向、"
        "硬切关系和跨镜连续性，不得遗漏或替换任何已在场景脚本中明确的执行细节"
        for index in range(1, 11)
    )
    raw_text = (
        "风格设定：现代家居个护广告，白色、米色和暖木色，柔和自然侧光。\n"
        "场景脚本：\n"
        + "\n".join(shots)
        + "\n技术约束：目标时长10秒；画幅9:16；分辨率720p；"
        "适配目标模型Seedance 1.5 Pro；单段提示词预算约1800字符；"
        "同一女性、服装、洗脸巾包装和珍珠纹理跨镜头保持一致；"
        "用户指定卖点文字：干湿两用，按原文准确显示；"
        "用户指定音效：声音：持续背景音乐；声音：持续背景音乐；"
        f"必须完整执行且不得替换的原始动作要求（按顺序）：{inventory}"
    )
    assert len(raw_text) > 1800

    result = compile_video_prompt(
        {
            "input_mode": "direct_input",
            "user_instruction": raw_text,
            "raw_text": raw_text,
            "assembled_text": raw_text,
            "final_text": raw_text,
        },
        duration=10,
        model_id="doubao-seedance-1-5-pro-251215",
        provider="volcengine_ark",
        references=[{"role": "product", "url": "https://example.com/product.png"}],
        product_reference=True,
        fit_mode="single_clip",
    )

    assert result["metadata"]["prompt_mode"] == "direct_compacted"
    assert result["metadata"]["prompt_over_budget"] is False
    assert result["metadata"]["prompt_char_count"] <= 1800
    assert result["metadata"]["shot_count"] == 10
    assert result["sequence_required"] is False
    assert result["plan"]["post_overlays"] == ["干湿两用"]
    assert result["plan"]["sfx"] == ["持续背景音乐"]
    assert "上传产品图为唯一商品主体" in result["prompt"]
    for metadata_text in (
        "目标时长",
        "画幅9:16",
        "分辨率720p",
        "适配目标模型",
        "单段提示词预算",
        "必须完整执行且不得替换的原始动作要求",
    ):
        assert metadata_text not in result["prompt"]


def test_seedance_execution_prompt_separates_packaging_ocr_and_neutralizes_actions():
    packaging_ocr = "DAMAH DARK MAGIC DAMAH 4TH GEN 166PCS"
    shots = [
        "Shot 1：墙上倒挂DAMAH洗脸巾包装，手指从底部向下拉拽抽出一张；"
        f"画面文字：{packaging_ocr}；硬切。声音：持续背景音乐",
        "Shot 2：双手在水槽上方握住湿润洗脸巾用力拧干，水滴滴落；"
        "画面文字：DAMAH 干湿两用 厚实吸水；硬切。声音：持续背景音乐",
        "Shot 3：女性用洗脸巾轻拭脸颊；画面文字：亲肤；硬切。声音：持续背景音乐",
        "Shot 4：双手紧握洗脸巾向外用力拉扯，绷紧至出现撕裂口，展示韧性；"
        f"画面文字：耐拉扯 不易掉絮 {packaging_ocr}；硬切。声音：持续背景音乐",
    ]
    raw_text = (
        "风格设定：温馨家居个护广告，柔和自然侧光。\n场景脚本：\n"
        + "\n".join(shots)
        + "\n技术约束：包装上印刷文字（"
        + packaging_ocr
        + "）跨镜头保持一致；包装文字准确保留，仅生成指定卖点字幕；"
        + "人物、包装和洗脸巾纹理跨镜头保持一致；"
        + "必须完整执行且不得替换的原始动作要求（按顺序）："
        + "；".join("重复镜头证据和执行约束" for _ in range(180))
    )

    result = compile_video_prompt(
        {
            "input_mode": "direct_input",
            "user_instruction": raw_text,
            "raw_text": raw_text,
            "assembled_text": raw_text,
            "final_text": raw_text,
        },
        duration=10,
        model_id="doubao-seedance-1-5-pro-251215",
        provider="volcengine_ark",
        references=[{"role": "first_frame", "url": "https://example.com/first.png"}],
        fit_mode="single_clip",
    )

    assert result["plan"]["post_overlays"] == [
        "干湿两用 厚实吸水",
        "亲肤",
        "耐拉扯 不易掉絮",
    ]
    assert result["metadata"]["packaging_overlay_cleaned_count"] == 3
    assert result["metadata"]["packaging_ocr_literal_removed_count"] >= 1
    assert result["metadata"]["packaging_brand_literal_removed_count"] >= 1
    assert result["metadata"]["model_overlay_instruction_removed_count"] >= 1
    assert result["metadata"]["embedded_text_overlays"] is False
    assert result["metadata"]["execution_language_normalized"] is True
    assert packaging_ocr not in result["prompt"]
    assert "DAMAH" not in result["prompt"]
    assert "包装上印刷文字跨镜头保持一致" in result["prompt"]
    assert "向下拉拽抽出" not in result["prompt"]
    assert "用力拧干" not in result["prompt"]
    assert "用力拉扯" not in result["prompt"]
    assert "撕裂口" not in result["prompt"]
    assert "洗脸巾" not in result["prompt"]
    assert "洁面巾" in result["prompt"]
    assert "向下抽出" in result["prompt"]
    assert "缓慢拧出水分" in result["prompt"]
    assert "纤维断面" in result["prompt"]
