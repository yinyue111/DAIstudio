"""编译器消费证据门三态运动信息（evidence_gate）的行为契约。

上游 gateway_prompting.constrain_video_shots_to_evidence 在每个 shot 上写：
- evidence_gate: {action/camera/transition: {verified, confidence, score, source, reason}}
- camera_motion_summary / cut_transition_evidence_refs / analyzer_status
编译器必须：verified 与 vlm_only 都以可执行原文写入，可信度只保留在
verified=false 结构化标记中；ffmpeg 硬切编译成剪辑节奏描述；camera 冲突
以分析器标签为准；全空时降级到 final_text 而不是崩溃。
"""
from app.services.video_prompt_compiler import (
    HARD_CUT_RHYTHM_CLAUSE,
    UNVERIFIED_EVIDENCE_SUFFIX,
    compile_video_prompt,
    store_video_prompt_compile,
)


def _gate_entry(verified, confidence=None, score=None, source=None, reason=None):
    return {
        "verified": verified,
        "confidence": confidence,
        "score": score,
        "source": source,
        "reason": reason,
    }


def _shot(**overrides):
    shot = {
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "visual": "女主站在浴室镜前",
        "lighting": "",
        "action": "",
        "camera": "",
        "transition": "",
        "analyzer_status": {
            "action": "unsupported",
            "camera_motion": "unsupported",
            "shot_transitions": "unsupported",
        },
        "evidence_gate": {
            "action": _gate_entry(False),
            "camera": _gate_entry(False),
            "transition": _gate_entry(False),
        },
    }
    shot.update(overrides)
    return shot


def _compile(shots, **structured):
    raw = {"风格": "高端日系个护广告", "video_analysis": {"shots": shots}}
    raw.update(structured)
    return compile_video_prompt(raw, duration=10, model_id="seedance-2.0")


def test_verified_action_compiles_without_hedging_and_marks_verified_true():
    result = _compile([
        _shot(
            action="从包装下方抽出一张洗脸巾",
            evidence_gate={
                "action": _gate_entry(
                    True, confidence="analyzer", source="semantic_provider"
                ),
                "camera": _gate_entry(False),
                "transition": _gate_entry(False),
            },
        )
    ])

    assert "从包装下方抽出一张洗脸巾" in result["prompt"]
    assert UNVERIFIED_EVIDENCE_SUFFIX not in result["prompt"]
    entry = result["metadata"]["shot_evidence"][0]["action"]
    assert entry["verified"] is True
    assert entry["confidence"] == "analyzer"
    assert entry["source"] == "semantic_provider"


def test_vlm_only_action_stays_executable_while_metadata_marks_verified_false():
    result = _compile([
        _shot(
            action="手指捏住洗脸巾边缘缓慢展开",
            evidence_gate={
                "action": _gate_entry(
                    False,
                    confidence="vlm_only",
                    source="cross_frame_vlm",
                    reason="语义动作分析器不可用，动作描述由多个不同时间戳抽样帧支撑但未经独立验证",
                ),
                "camera": _gate_entry(False),
                "transition": _gate_entry(False),
            },
        )
    ])

    assert "手指捏住洗脸巾边缘缓慢展开" in result["prompt"]
    assert UNVERIFIED_EVIDENCE_SUFFIX not in result["prompt"]
    entry = result["metadata"]["shot_evidence"][0]["action"]
    assert entry["verified"] is False
    assert entry["confidence"] == "vlm_only"
    assert entry["reason"]
    assert entry["prompt_text"] == "手指捏住洗脸巾边缘缓慢展开"
    # plan 与 metadata 一致，前端可从任一侧读取可信度差异。
    assert result["plan"]["shot_evidence"] == result["metadata"]["shot_evidence"]


def test_cleared_action_never_reaches_the_prompt():
    result = _compile([
        _shot(
            visual="包装置于白色圆桌中央",
            action="",
            evidence_gate={
                "action": _gate_entry(
                    False, reason="语义动作分析器不可用且缺乏跨帧证据，动作描述已清除"
                ),
                "camera": _gate_entry(False),
                "transition": _gate_entry(False),
            },
        )
    ])

    assert "包装置于白色圆桌中央" in result["prompt"]
    entry = result["metadata"]["shot_evidence"][0]["action"]
    assert entry["verified"] is False
    assert entry["text"] == ""
    assert entry["prompt_text"] == ""


def test_ffmpeg_hard_cut_compiles_into_editing_rhythm():
    result = _compile([
        _shot(
            action="抽出洗脸巾",
            transition="硬切",
            cut_transition_evidence_refs=["cut-1"],
            analyzer_status={
                "action": "analyzed",
                "camera_motion": "unsupported",
                "shot_transitions": "analyzed",
            },
            evidence_gate={
                "action": _gate_entry(
                    True, confidence="analyzer", source="semantic_provider"
                ),
                "camera": _gate_entry(False),
                "transition": _gate_entry(
                    True, confidence="analyzer", score=0.93, source="ffmpeg_scene"
                ),
            },
        ),
        _shot(
            start_seconds=2.0,
            end_seconds=4.0,
            visual="微距展示云纹",
        ),
    ])

    assert "干净硬切" in result["prompt"]
    assert HARD_CUT_RHYTHM_CLAUSE in result["prompt"]
    entry = result["metadata"]["shot_evidence"][0]["transition"]
    assert entry["verified"] is True
    assert entry["source"] == "ffmpeg_scene"
    assert entry["score"] == 0.93


def test_camera_conflict_prefers_analyzer_label_over_vlm_text():
    result = _compile([
        _shot(
            camera="镜头缓慢推近",
            camera_motion_summary={
                "status": "analyzed",
                "dominant_label": "pan",
                "dominant_direction": "right",
                "confidence": 0.85,
                "label_scores": {"pan": 0.85, "zoom": 0.05},
            },
            camera_motion_evidence_refs=["cam-1"],
            analyzer_status={
                "action": "unsupported",
                "camera_motion": "analyzed",
                "shot_transitions": "unsupported",
            },
            evidence_gate={
                "action": _gate_entry(False),
                # 模拟未完成对账的上游：门放行了 VLM 原文，编译器兜底替换。
                "camera": _gate_entry(
                    True, confidence="analyzer", score=0.85, source="analyzer_status"
                ),
                "transition": _gate_entry(False),
            },
        )
    ])

    assert "镜头向右横摇" in result["prompt"]
    assert "推近" not in result["prompt"]
    entry = result["metadata"]["shot_evidence"][0]["camera"]
    assert entry["verified"] is True
    assert entry["source"] == "opencv_lk_homography"
    assert entry["prompt_text"] == "镜头向右横摇"


def test_camera_agreeing_with_analyzer_keeps_vlm_wording():
    result = _compile([
        _shot(
            camera="镜头缓慢推近产品",
            camera_motion_summary={
                "status": "analyzed",
                "dominant_label": "zoom",
                "dominant_direction": "in",
                "confidence": 0.8,
            },
            evidence_gate={
                "action": _gate_entry(False),
                "camera": _gate_entry(
                    True, confidence="analyzer", score=0.8, source="opencv_lk_homography"
                ),
                "transition": _gate_entry(False),
            },
        )
    ])

    assert "镜头缓慢推近产品" in result["prompt"]


def test_vlm_only_camera_stays_executable_without_evidence_annotation():
    result = _compile([
        _shot(
            camera="镜头带一点呼吸感的轻微晃动",
            evidence_gate={
                "action": _gate_entry(False),
                "camera": _gate_entry(
                    False,
                    confidence="vlm_only",
                    score=0.2,
                    source="opencv_lk_homography",
                    reason="光流运镜分类置信度过低，VLM 运镜描述未经对账、降置信度保留",
                ),
                "transition": _gate_entry(False),
            },
        )
    ])

    assert "镜头带一点呼吸感的轻微晃动" in result["prompt"]
    assert UNVERIFIED_EVIDENCE_SUFFIX not in result["prompt"]
    entry = result["metadata"]["shot_evidence"][0]["camera"]
    assert entry["verified"] is False
    assert entry["confidence"] == "vlm_only"


def test_source_shot_timestamps_stay_in_metadata_not_executable_prompt():
    result = _compile([
        _shot(
            start_seconds=12.25,
            end_seconds=14.75,
            action="从包装下方抽出洗脸巾",
            evidence_gate={
                "action": _gate_entry(True, confidence="analyzer"),
                "camera": _gate_entry(False),
                "transition": _gate_entry(False),
            },
        )
    ])

    assert "12.250-14.750s" not in result["prompt"]
    evidence = result["metadata"]["shot_evidence"][0]
    assert evidence["start_seconds"] == 12.25
    assert evidence["end_seconds"] == 14.75


def test_all_cleared_shots_degrade_to_final_text_without_crashing():
    result = _compile(
        [_shot(visual=""), _shot(visual="", start_seconds=2.0, end_seconds=4.0)],
        final_text="产品静置在木质台面，柔和晨光",
    )

    assert result["prompt"]
    assert "产品静置在木质台面" in result["prompt"]
    # 判定记录仍保留（included=False），前端可解释为什么没有时序分镜。
    evidence = result["metadata"]["shot_evidence"]
    assert len(evidence) == 2
    assert all(entry["included"] is False for entry in evidence)
    assert all(entry["action"]["verified"] is False for entry in evidence)


def test_user_instruction_still_wins_over_evidence_shots():
    result = _compile(
        [
            _shot(
                action="旧的证据动作",
                evidence_gate={
                    "action": _gate_entry(True, confidence="analyzer"),
                    "camera": _gate_entry(False),
                    "transition": _gate_entry(False),
                },
            )
        ],
        user_instruction="Shot 1：手持产品稳定入镜；Shot 2：慢速推近 Logo 特写。",
    )

    assert "旧的证据动作" not in result["prompt"]
    assert "手持产品稳定入镜" in result["prompt"]
    assert result["metadata"]["shot_evidence"] == []


def test_evidence_shots_override_stale_string_timeline():
    result = _compile(
        [
            _shot(
                action="从包装下方抽出一张洗脸巾",
                evidence_gate={
                    "action": _gate_entry(
                        True, confidence="analyzer", source="semantic_provider"
                    ),
                    "camera": _gate_entry(False),
                    "transition": _gate_entry(False),
                },
            )
        ],
        **{"时序分镜": "0-4s 旧时间轴：产品翻转展示"},
    )

    assert "旧时间轴" not in result["prompt"]
    assert "从包装下方抽出一张洗脸巾" in result["prompt"]


def test_gated_dicts_inside_timeline_list_are_compiled_not_streamed_as_repr():
    result = compile_video_prompt(
        {
            "风格": "写实产品广告",
            "时序分镜": [
                _shot(
                    action="拿起产品对准镜头",
                    evidence_gate={
                        "action": _gate_entry(
                            True, confidence="analyzer", source="semantic_provider"
                        ),
                        "camera": _gate_entry(False),
                        "transition": _gate_entry(False),
                    },
                )
            ],
        },
        duration=10,
        model_id="seedance-2.0",
    )

    assert "拿起产品对准镜头" in result["prompt"]
    assert "evidence_gate" not in result["prompt"]
    assert "{" not in result["prompt"]


def test_store_video_prompt_compile_persists_shot_evidence():
    result = _compile([
        _shot(
            action="手指捏住边缘展开",
            evidence_gate={
                "action": _gate_entry(False, confidence="vlm_only", source="cross_frame_vlm"),
                "camera": _gate_entry(False),
                "transition": _gate_entry(False),
            },
        )
    ])
    params = store_video_prompt_compile({}, result, [])

    assert params["_video_shot_evidence"] == result["metadata"]["shot_evidence"]
    assert params["_video_shot_evidence"][0]["action"]["verified"] is False
