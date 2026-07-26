"""反推清洗链路的 golden 回归测试。

反推质量由一串"会改写/删除模型输出"的清洗与约束点决定（清单见各测试类
docstring）。这些正则和门逻辑改一个字符就可能悄悄改变反推结果，本文件把
每个清洗点的边界行为锁定成 golden 用例：改动实现后任何用例失败，都说明
反推输出被改变，必须人工确认是修复还是回归。

已知缺陷用 strict xfail 标记：缺陷被修复时用例会 XPASS 失败，提醒同步
更新期望值并移除标记。实现文件归其他工作流所有，本文件只做防线。
"""
import json
from pathlib import Path

import pytest

from app.services import gateway_prompting as gp
from app.services.gateway_prompting import (
    ReverseResultValidationError,
    clean_visual_generation_clause,
    compose_visual_final_text,
    constrain_video_shots_to_evidence,
    normalize_video_shots,
    sanitize_video_audio_evidence,
    validate_reverse_result,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reverse_sanitization_clauses.json"


def _load_clause_cases() -> list[dict]:
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return payload["cases"]


@pytest.mark.parametrize(
    "case",
    _load_clause_cases(),
    ids=lambda case: case["id"],
)
def test_clean_visual_clause_golden(case):
    """子句清洗（占位/前缀/参考序号/平台归因/质量词/色值/百分比/置信度/分句删除）。"""
    assert clean_visual_generation_clause(case["input"]) == case["expected"], case["note"]


def test_clean_visual_clause_non_string_inputs_become_empty():
    """非字符串输入一律清空，不抛异常。"""
    for value in (None, 123, 1.5, True, [], {}, object()):
        assert clean_visual_generation_clause(value) == ""


class TestUncertaintyCollateralDeletion:
    """gateway_prompting.py 不确定措辞的删除粒度。

    连坐删除缺陷已修复：先按句末标点（；;。.!！?？\\n）分句，含不确定词的
    句子再按逗号/顿号切成子句逐段判断，只删含不确定措辞的子句，同句里的
    有效观察保留（整个子句丢弃而非词级挖除，避免留下残句碎片）。
    """

    def test_valid_observation_survives_same_sentence_uncertainty(self):
        """逗号前的有效观察保留，只删"看不清"所在子句。"""
        result = clean_visual_generation_clause("主体为红色圆瓶居中，标签文字看不清")
        assert result == "主体为红色圆瓶居中"

    def test_all_uncertain_subclauses_drop_whole_sentence(self):
        """整句所有子句都含不确定措辞时仍整体删除，不留碎片。"""
        assert clean_visual_generation_clause("疑似玻璃材质，标签文字看不清") == ""

    def test_platform_attribution_should_not_swallow_following_clause(self):
        """归因正则终止边界已含逗号/顿号：只删归因子句，后续观察保留。"""
        result = clean_visual_generation_clause("适合用于小红书种草图，主体居中")
        assert result == "主体居中"

    def test_platform_attribution_swallow_current_behavior_pinned(self):
        """归因居中时前后有效子句都保留（归因删除后残留逗号被清理）。"""
        assert (
            clean_visual_generation_clause("背景为米白台面，可用于小红书种草图，主体居中")
            == "背景为米白台面，主体居中"
        )

    def test_multi_sentence_mixed_uncertainty_keeps_each_valid_sentence(self):
        """句号分隔时删除粒度正确：只删不确定句。"""
        text = "瓶身为磨砂玻璃。品牌可能是进口的。柔和顶光从左上打下"
        assert clean_visual_generation_clause(text) == "瓶身为磨砂玻璃。柔和顶光从左上打下"


class TestTruncateVisualClause:
    """gateway_prompting.py:500-507 的子句截断：优先在标点处断，避免残句。"""

    def test_short_text_untouched(self):
        assert gp._truncate_visual_clause("一二三四五六七八九十", 20) == "一二三四五六七八九十"

    def test_cut_at_punctuation_boundary(self):
        text = "红色圆瓶居中摆放于米白台面，柔和顶光从左上方打下，背景虚化"
        # limit=20 时最近的逗号在第 13 位（>= 12 的下限），应在逗号处截断
        assert gp._truncate_visual_clause(text, 20) == "红色圆瓶居中摆放于米白台面"

    def test_no_punctuation_hard_cut(self):
        text = "这是一个没有任何标点的超长中文描述文本内容持续不断延伸"
        assert gp._truncate_visual_clause(text, 20) == text[:20]

    def test_early_punctuation_ignored_to_avoid_tiny_fragment(self):
        # 标点太靠前（< max(12, limit//2)）时不回退，直接硬切
        text = "红瓶，居中摆放于米白台面上光线柔和自然"
        assert gp._truncate_visual_clause(text, 14) == "红瓶，居中摆放于米白台面上光"


class TestFitVisualPrompt:
    """gateway_prompting.py:510-538 的预算拼接：分号切分、去重、超预算整句跳过。"""

    def test_deduplicates_repeated_clauses(self):
        assert gp._fit_visual_prompt(["主体居中", "主体居中", "光线柔和"], "video") == (
            "主体居中；光线柔和"
        )

    def test_splits_parts_on_semicolons_before_dedupe(self):
        assert gp._fit_visual_prompt(["主体居中；主体居中；光线柔和"], "video") == (
            "主体居中；光线柔和"
        )

    def test_budget_skips_long_clause_but_keeps_shorter_later_one(self):
        # video 预算 220、单句上限 64：三个 64 字句共占 194，第四个 64 字句
        # 放不下应整句跳过，而更短的低优先级句仍可入选，不得截成残句。
        long_a = "场景" + "一" * 62
        long_b = "光线" + "二" * 62
        long_c = "材质" + "三" * 62
        long_d = "构图" + "四" * 62
        short_e = "氛围宁静"
        result = gp._fit_visual_prompt([long_a, long_b, long_c, long_d, short_e], "video")
        parts = result.split("；")
        assert parts[:3] == [long_a[:64], long_b[:64], long_c[:64]]
        assert long_d[:64] not in parts
        assert short_e in parts
        assert len(result) <= 220

    def test_placeholder_parts_dropped(self):
        assert gp._fit_visual_prompt(["未见", "无 / 不适用", "主体居中"], "video") == "主体居中"


class TestEvidenceGateSymmetry:
    """gateway_prompting.py constrain_video_shots_to_evidence 的证据门。

    对称化后的规则：
    - action：analyzed/partial 且 refs 非空放行；unsupported 但满足跨帧
      证据契约（≥2 个不同证据帧）降置信度保留（vlm_only）；否则清空；
    - camera：有 camera_motion_summary 时做标签级对账（冲突以分析器为准）；
      无 summary 的旧数据退回存在性验证；
    - transition：语义 provider 或 ffmpeg 场景切点任一背书即放行；
    - visual/lighting 只做子句清洗。
    对称化行为的完整用例见 test_evidence_gate_symmetry.py，本类锁定基线。
    """

    @staticmethod
    def _shot(**overrides):
        shot = {
            "visual": "红色圆瓶居中",
            "lighting": "柔和顶光",
            "action": "瓶身缓慢旋转",
            "camera": "镜头缓慢推近",
            "transition": "硬切到特写",
            "analyzer_status": {
                "action": "analyzed",
                "camera_motion": "analyzed",
                "transition": "analyzed",
            },
            "action_evidence_refs": ["m1"],
            "camera_motion_evidence_refs": ["c1"],
            "transition_evidence_refs": ["t1"],
        }
        shot.update(overrides)
        return shot

    def test_verified_fields_pass_through(self):
        out = constrain_video_shots_to_evidence([self._shot()])[0]
        assert out["action"] == "瓶身缓慢旋转"
        assert out["camera"] == "镜头缓慢推近"
        assert out["transition"] == "硬切到特写"

    def test_partial_status_with_refs_passes(self):
        shot = self._shot(analyzer_status={
            "action": "partial", "camera_motion": "partial", "transition": "partial",
        })
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["action"] == "瓶身缓慢旋转"

    def test_unsupported_status_clears_field(self):
        shot = self._shot(analyzer_status={
            "action": "unsupported", "camera_motion": "analyzed", "transition": "failed",
        })
        out = constrain_video_shots_to_evidence([shot])[0]
        # 语义分析 unsupported 且本 shot 无跨帧证据（无 evidence_frame_indices）
        # 时 action 仍被清空；带跨帧证据的降级保留见 test_evidence_gate_symmetry.py。
        assert out["action"] == ""
        assert out["camera"] == "镜头缓慢推近"
        assert out["transition"] == ""

    def test_analyzed_status_without_refs_clears_field(self):
        shot = self._shot(action_evidence_refs=[])
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["action"] == ""
        assert out["camera"] == "镜头缓慢推近"

    def test_missing_analyzer_status_clears_all_temporal_fields(self):
        out = constrain_video_shots_to_evidence([
            {"visual": "红瓶", "action": "旋转", "camera": "推近", "transition": "淡出"},
        ])[0]
        assert (out["action"], out["camera"], out["transition"]) == ("", "", "")
        assert out["visual"] == "红瓶"

    def test_visual_and_lighting_still_sanitized(self):
        shot = self._shot(visual="未见", lighting="直接可见事实：柔和顶光")
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["visual"] == ""
        assert out["lighting"] == "柔和顶光"

    def test_non_dict_and_empty_inputs(self):
        assert constrain_video_shots_to_evidence(None) == []
        assert constrain_video_shots_to_evidence(["not-a-dict", 42]) == []


class TestNormalizeVideoShots:
    """gateway_prompting.py:1318-1495：单帧证据擦除运动字段、无效帧/零置信度丢弃。"""

    def test_single_frame_evidence_wipes_motion_fields(self):
        shots = normalize_video_shots(
            [{
                "start_seconds": 0, "end_seconds": 1, "visual": "红瓶", "lighting": "顶光",
                "action": "旋转", "camera": "推近", "transition": "淡出", "ocr": "",
                "evidence_frame_indices": [1], "confidence": 0.9,
            }],
            duration_seconds=10, frame_count=4,
        )
        assert shots[0]["visual"] == "红瓶"
        assert shots[0]["lighting"] == "顶光"
        assert (shots[0]["action"], shots[0]["camera"], shots[0]["transition"]) == ("", "", "")

    def test_cross_frame_evidence_keeps_action(self):
        shots = normalize_video_shots(
            [{
                "start_seconds": 0, "end_seconds": 1, "visual": "红瓶",
                "action": "旋转", "evidence_frame_indices": [1, 2], "confidence": 0.9,
            }],
            duration_seconds=10, frame_count=4,
        )
        assert shots[0]["action"] == "旋转"

    def test_zero_confidence_shot_dropped(self):
        assert normalize_video_shots(
            [{
                "start_seconds": 0, "end_seconds": 1, "visual": "红瓶",
                "evidence_frame_indices": [1], "confidence": 0,
            }],
            duration_seconds=10, frame_count=4,
        ) == []

    def test_out_of_range_frame_indices_filtered_then_shot_dropped(self):
        assert normalize_video_shots(
            [{
                "start_seconds": 0, "end_seconds": 1, "visual": "红瓶",
                "evidence_frame_indices": [9, 10], "confidence": 0.9,
            }],
            duration_seconds=10, frame_count=4,
        ) == []


class TestValidateReverseResultGolden:
    """validate_reverse_result 的端到端 golden：字段白名单合成 + 服务端事实归一。"""

    def test_video_full_pipeline_final_text_and_confidence_backfill(self):
        payload = {
            "图像类型": "产品视频",
            "主体": "红色圆瓶，居中",
            "旁白": "买它买它",
            "shots": [{
                "start_seconds": 0.0, "end_seconds": 2.0, "visual": "红色圆瓶特写",
                "action": "瓶身缓慢旋转", "camera": "", "lighting": "柔和顶光",
                "transition": "", "ocr": "", "audio_cue": "",
                "evidence_frame_indices": [1, 2], "confidence": 0.0,
            }],
            "final_text": "provider 自述文本",
        }
        out = validate_reverse_result(
            payload, "video",
            required_video_frame_indices=[1, 2],
            video_frame_timestamps={1: 0.0, 2: 2.0},
        )
        # 旁白不进白名单；shot 细节按 visual/action/lighting 顺序拼接并带时间戳
        assert out["final_text"] == (
            "红色圆瓶，居中；镜头1（0.000-2.000秒）：红色圆瓶特写，瓶身缓慢旋转，柔和顶光"
        )
        # provider 抄了 schema 占位 confidence=0：有帧证据的 visual 不作废，回填中性 0.5
        assert out["shots"][0]["confidence"] == 0.5
        # provider final_text 只留审计，不作为生成提示词
        assert out["provider_final_text"] == "provider 自述文本"

    def test_single_frame_shot_time_span_narrowed_to_frame_timestamp(self):
        payload = {
            "主体": "红色圆瓶",
            "shots": [{
                "start_seconds": 0.0, "end_seconds": 9.0, "visual": "红色圆瓶特写",
                "evidence_frame_indices": [2], "confidence": 0.8,
            }],
            "final_text": "x",
        }
        out = validate_reverse_result(
            payload, "video",
            required_video_frame_indices=[2],
            video_frame_timestamps={2: 4.0},
        )
        # 单帧支撑不了 9 秒时长声明：收窄到帧时间戳 ±0.75 秒，画面证据保留
        assert out["shots"][0]["start_seconds"] == 3.25
        assert out["shots"][0]["end_seconds"] == 4.75

    def test_unexpected_frame_index_rejected(self):
        payload = {
            "主体": "红色圆瓶",
            "shots": [{
                "start_seconds": 0.0, "end_seconds": 1.0, "visual": "红瓶",
                "evidence_frame_indices": [7], "confidence": 0.8,
            }],
            "final_text": "x",
        }
        with pytest.raises(ReverseResultValidationError, match="超出本次采样帧范围"):
            validate_reverse_result(payload, "video", required_video_frame_indices=[1, 2])

    def test_decode_rejects_trailing_content_and_accepts_bom(self):
        with pytest.raises(ReverseResultValidationError, match="额外非空内容"):
            gp._decode_json_object('{"a": 1} extra')
        assert gp._decode_json_object('﻿ {"a": 1}') == {"a": 1}


class TestComposeVisualFinalTextWhitelist:
    """compose_visual_final_text 的字段白名单与镜头压缩。"""

    def test_non_whitelisted_fields_never_reach_final_text(self):
        structured = {
            "图像类型": "产品视频",
            "主体": "红色圆瓶",
            "旁白": "画外音说买它",
            "字幕卖点": "全场五折",
            "负向": "形变，闪烁",
            "自定义字段": "不应出现",
        }
        assert compose_visual_final_text(structured, "video") == "红色圆瓶"

    def test_all_fields_filtered_raises(self):
        with pytest.raises(ReverseResultValidationError, match="视觉白名单字段"):
            compose_visual_final_text({"旁白": "只有旁白"}, "video")

    def test_shot_line_includes_segment_and_timing(self):
        structured = {"图像类型": "产品视频", "主体": "红瓶"}
        shots = [{
            "start_seconds": 0.0, "end_seconds": 2.0, "visual": "红瓶特写",
            "action": "旋转", "lighting": "顶光", "source_segment_index": 2,
        }]
        assert compose_visual_final_text(structured, "video", shots) == (
            "红瓶；片段2 镜头1（0.000-2.000秒）：红瓶特写，旋转，顶光"
        )

    def test_long_product_video_compressed_and_black_frames_dropped(self):
        structured = {"图像类型": "产品视频", "主体": "红瓶"}
        shots = [
            {"start_seconds": 0, "end_seconds": 6, "visual": "纯黑画面"},
            {"start_seconds": 6, "end_seconds": 12, "visual": "模特手持产品走入画面"},
            {"start_seconds": 12, "end_seconds": 18, "visual": "瓶盖与标签特写", "confidence": 0.9},
            {"start_seconds": 18, "end_seconds": 24, "visual": "产品正面完整居中展示", "confidence": 0.8},
        ]
        assert compose_visual_final_text(structured, "video", shots) == (
            "红瓶；按原镜头顺序压缩为单段核心版；镜头间干净硬切；"
            "镜头1：模特手持产品走入画面；镜头2：依次展示瓶盖与标签特写；"
            "镜头3：产品正面完整居中展示"
        )

    def test_image_field_level_truncation_limit(self):
        structured = {"图像类型": "产品图", "主体": "红" * 60}
        final_text = compose_visual_final_text(structured, "image")
        # 主体的图片档位上限是 48 字
        assert final_text.split("；")[0] == "红" * 48


class TestAudioEvidenceSanitization:
    """sanitize_video_audio_evidence：provider 音频散文一律替换为分析器证据或占位。"""

    def test_no_evidence_replaces_all_audio_prose(self):
        structured = {"旁白": "模型幻觉旁白", "音效": "模型幻觉音效"}
        shots = [{"start_seconds": 0, "end_seconds": 2, "audio_cue": "幻觉"}]
        statuses = sanitize_video_audio_evidence(structured, shots, audio_evidence=None)
        assert structured["旁白"] == "未分析"
        assert structured["音效"] == "未分析"
        assert shots[0]["audio_cue"] == "未分析"
        assert statuses["asr"] == "unsupported"

    def test_analyzed_asr_produces_timestamped_transcript(self):
        structured = {"旁白": "幻觉"}
        shots = [{"start_seconds": 0, "end_seconds": 2}]
        evidence = {
            "status": "analyzed",
            "features": {"asr": {"status": "analyzed"}},
            "segments": [{"start_seconds": 0.5, "end_seconds": 1.5, "text": "全新上市"}],
        }
        sanitize_video_audio_evidence(structured, shots, audio_evidence=evidence)
        assert structured["旁白"] == "[0.500-1.500秒] 全新上市"
        assert shots[0]["audio_cue"] == "对白/旁白：全新上市"
        # music/beat/sfx 未声明状态 → 未支持
        assert structured["音效"] == "未支持"


class TestComposeFinalFallback:
    """compose_final：provider 缺 final_text 时的兜底合成，排除集不得进提示词。"""

    def test_excluded_keys_skipped_and_kv_format(self):
        obj = {
            "主体": "红色圆瓶",
            "光线": "柔和顶光",
            "负向": "形变",
            "旁白": "买它",
            "图像类型": "产品图",
        }
        assert gp.compose_final(obj) == "主体: 红色圆瓶；光线: 柔和顶光"

    def test_empty_object_falls_back_to_safe_default(self):
        assert gp.compose_final({}) == "保持参考素材的主体、构图、光线和配色"
