from app.services.video_prompt_compiler import (
    build_video_prompt_references,
    compile_video_prompt,
    infer_video_model_profile,
)


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


def test_single_clip_product_action_keeps_locked_text_fidelity_guard():
    result = compile_video_prompt(
        "手从包装底部抽出洗脸巾；微距展开3D如意云纹",
        duration=10,
        product_reference=True,
        product_lock_mode="locked",
        fit_mode="single_clip",
    )

    assert "产品视频策略：单段动作展示" in result["prompt"]
    assert "避免快速旋转、翻面、强运动模糊、遮挡或裁切产品" in result["prompt"]


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
    assert "抽帧反推" in result["prompt"]
    assert "不进行原视频逐帧动作复刻" in result["prompt"]
    assert result["metadata"]["reference_roles"] == ["motion_analysis"]
    assert result["metadata"]["motion_reference_mode"] == "analysis_only"


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
