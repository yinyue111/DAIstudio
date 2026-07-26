"""证据门对称化（constrain_video_shots_to_evidence）的行为用例。

评估结论：旧门对 action/transition 过严（语义 provider 缺席时全清空，
运动维度归零）、对 camera 过松（纯时间窗重叠即放行，光流分类结果零引用）。
本文件锁定对称化后的三条规则与置信度标注契约：

- camera：与 camera_motion_summary 的 dominant_label 做标签级对账，
  一致保留、冲突以分析器为准、分类置信度过低降级保留（vlm_only）；
- action：三态——语义分析器背书放行；unsupported 但满足跨帧证据契约
  （≥2 个不同证据帧）降置信度保留并标注 vlm_only；否则清空；
- transition：ffmpeg 场景切点（shot_transitions analyzed 且
  cut_transition_evidence_refs 非空）是强证据，可放行并替换软转场声称；
- 每个 shot 附带 evidence_gate，UI/下游依 verified+confidence 区分等级。
"""
import pytest

from app.services.gateway_prompting import (
    clean_visual_generation_clause,
    constrain_video_shots_to_evidence,
)


def _camera_summary(
    label,
    *,
    confidence=0.8,
    direction=None,
    status="analyzed",
    sample_count=3,
):
    scores = {"pan": 0.05, "tilt": 0.05, "zoom": 0.05, "static": 0.05}
    if label:
        scores[label] = confidence
    return {
        "status": status,
        "analyzer": "opencv_lk_homography",
        "analyzer_version": "lk-homography-v1",
        "camera_labels": ["pan", "tilt", "zoom", "static"],
        "sample_count": sample_count,
        "evidence_refs": ["motion-a", "motion-b"][:sample_count],
        "dominant_label": label,
        "dominant_direction": direction,
        "label_scores": scores if label else None,
        "confidence": confidence if label else None,
    }


def _shot(**overrides):
    shot = {
        "visual": "红色圆瓶居中",
        "lighting": "柔和顶光",
        "action": "",
        "camera": "",
        "transition": "",
        "analyzer_status": {
            "action": "unsupported",
            "camera_motion": "unsupported",
            "transition": "unsupported",
            "shot_transitions": "unsupported",
        },
        "action_evidence_refs": [],
        "camera_motion_evidence_refs": [],
        "transition_evidence_refs": [],
        "cut_transition_evidence_refs": [],
    }
    shot.update(overrides)
    return shot


def _gate(shot_out, field):
    gate = shot_out["evidence_gate"]
    assert set(gate) == {"action", "camera", "transition"}
    entry = gate[field]
    assert set(entry) == {"verified", "confidence", "score", "source", "reason"}
    return entry


class TestCameraLabelReconciliation:
    """camera：从存在性验证升级为与光流分类的标签级对账。"""

    def _camera_shot(self, text, summary, **overrides):
        return _shot(
            camera=text,
            camera_motion_summary=summary,
            camera_motion_evidence_refs=["motion-a"],
            analyzer_status={
                **_shot()["analyzer_status"],
                "camera_motion": "analyzed",
            },
            **overrides,
        )

    def test_consistent_label_keeps_vlm_text_verified(self):
        shot = self._camera_shot(
            "镜头向右横摇扫过产品",
            _camera_summary("pan", confidence=0.82, direction="right"),
        )
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["camera"] == "镜头向右横摇扫过产品"
        entry = _gate(out, "camera")
        assert entry["verified"] is True
        assert entry["confidence"] == "analyzer"
        assert entry["score"] == pytest.approx(0.82)
        assert entry["source"] == "opencv_lk_homography"
        assert entry["reason"] is None

    def test_static_dominant_overrides_vlm_orbit_hallucination(self):
        """评估点名的失败模式：cv2 判定静止、VLM 幻觉"缓慢环绕推近"。"""
        shot = self._camera_shot(
            "镜头缓慢环绕推近产品",
            _camera_summary("static", confidence=0.91),
        )
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["camera"] == "固定镜头"
        entry = _gate(out, "camera")
        assert entry["verified"] is True
        assert entry["confidence"] == "analyzer"
        assert "冲突" in entry["reason"]

    def test_conflicting_motion_type_replaced_with_analyzer_clause(self):
        shot = self._camera_shot(
            "镜头缓慢推近",
            _camera_summary("pan", confidence=0.7, direction="left"),
        )
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["camera"] == "镜头向左横摇"
        entry = _gate(out, "camera")
        assert entry["verified"] is True
        assert "冲突" in entry["reason"]
        assert "pan" in entry["reason"]

    def test_low_confidence_classification_keeps_text_as_vlm_only(self):
        """光流分类置信度低于阈值：不背书也不否决，降级保留并如实标注。"""
        shot = self._camera_shot(
            "镜头缓慢推近",
            _camera_summary("zoom", confidence=0.2, direction="in"),
        )
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["camera"] == "镜头缓慢推近"
        entry = _gate(out, "camera")
        assert entry["verified"] is False
        assert entry["confidence"] == "vlm_only"
        assert "置信度过低" in entry["reason"]

    def test_unclassifiable_vlm_text_kept_but_downgraded(self):
        shot = self._camera_shot(
            "镜头语言富有张力",
            _camera_summary("pan", confidence=0.75, direction="right"),
        )
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["camera"] == "镜头语言富有张力"
        entry = _gate(out, "camera")
        assert entry["verified"] is False
        assert entry["confidence"] == "vlm_only"

    def test_missing_vlm_text_backfilled_from_analyzer(self):
        shot = self._camera_shot(
            "",
            _camera_summary("zoom", confidence=0.88, direction="in"),
        )
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["camera"] == "镜头缓慢推近"
        entry = _gate(out, "camera")
        assert entry["verified"] is True
        assert entry["reason"] == "VLM 未描述运镜，由光流分析结论补齐"

    def test_legacy_shot_without_summary_falls_back_to_existence_rule(self):
        """旧数据没有 camera_motion_summary：保持原有存在性放行，不回归。"""
        shot = _shot(
            camera="镜头缓慢推近",
            camera_motion_evidence_refs=["motion-a"],
            analyzer_status={
                **_shot()["analyzer_status"],
                "camera_motion": "analyzed",
            },
        )
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["camera"] == "镜头缓慢推近"
        entry = _gate(out, "camera")
        assert entry["verified"] is True
        assert entry["source"] == "analyzer_status"

    def test_no_motion_refs_still_clears_camera(self):
        shot = _shot(camera="镜头缓慢推近")
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["camera"] == ""
        assert _gate(out, "camera")["verified"] is False


class TestActionThreeState:
    """action：二值门改三态，恢复被误杀的跨帧运动信息。"""

    def test_semantic_analyzer_backed_action_verified(self):
        shot = _shot(
            action="模特抬手展示瓶身",
            action_evidence_refs=["action-1"],
            analyzer_status={**_shot()["analyzer_status"], "action": "analyzed"},
        )
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["action"] == "模特抬手展示瓶身"
        entry = _gate(out, "action")
        assert entry["verified"] is True
        assert entry["confidence"] == "analyzer"
        assert entry["source"] == "semantic_provider"

    def test_unsupported_with_cross_frame_evidence_kept_as_vlm_only(self):
        """语义分析器缺席（semantic_url 未配置）但满足跨帧契约：降级保留。"""
        shot = _shot(
            action="瓶身缓慢旋转半周",
            evidence_frame_indices=[3, 7],
        )
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["action"] == "瓶身缓慢旋转半周"
        entry = _gate(out, "action")
        assert entry["verified"] is False
        assert entry["confidence"] == "vlm_only"
        assert entry["source"] == "cross_frame_vlm"
        assert "未经独立验证" in entry["reason"]

    def test_unsupported_single_frame_still_cleared(self):
        shot = _shot(action="瓶身缓慢旋转", evidence_frame_indices=[4])
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["action"] == ""
        assert _gate(out, "action")["verified"] is False

    def test_unsupported_duplicate_frame_indices_not_cross_frame(self):
        shot = _shot(action="瓶身缓慢旋转", evidence_frame_indices=[4, 4])
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["action"] == ""

    def test_unsupported_without_frame_evidence_cleared(self):
        shot = _shot(action="瓶身缓慢旋转")
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["action"] == ""


class TestTransitionFfmpegCuts:
    """transition：服务器确认的 ffmpeg 硬切是强证据。"""

    def _cut_shot(self, text, **overrides):
        return _shot(
            transition=text,
            cut_transition_evidence_refs=["cut-transition-abc"],
            analyzer_status={
                **_shot()["analyzer_status"],
                "shot_transitions": "analyzed",
            },
            **overrides,
        )

    def test_ffmpeg_cut_admits_vlm_hard_cut_text(self):
        out = constrain_video_shots_to_evidence([self._cut_shot("硬切到特写")])[0]
        assert out["transition"] == "硬切到特写"
        entry = _gate(out, "transition")
        assert entry["verified"] is True
        assert entry["confidence"] == "analyzer"
        assert entry["source"] == "ffmpeg_scene"

    def test_ffmpeg_cut_replaces_soft_transition_claim(self):
        out = constrain_video_shots_to_evidence([self._cut_shot("叠化过渡到水面")])[0]
        assert out["transition"] == "硬切"
        entry = _gate(out, "transition")
        assert entry["verified"] is True
        assert "软转场" in entry["reason"]

    def test_ffmpeg_cut_backfills_missing_vlm_text(self):
        out = constrain_video_shots_to_evidence([self._cut_shot("")])[0]
        assert out["transition"] == "硬切"
        assert _gate(out, "transition")["verified"] is True

    def test_evidence_payload_supplies_scene_score(self):
        evidence = {
            "shot_transitions": {
                "status": "analyzed",
                "events": [
                    {"evidence_id": "cut-transition-abc", "confidence": 0.83},
                ],
            },
        }
        out = constrain_video_shots_to_evidence(
            [self._cut_shot("硬切到特写")], evidence=evidence
        )[0]
        assert _gate(out, "transition")["score"] == pytest.approx(0.83)

    def test_unsupported_shot_transitions_status_does_not_admit(self):
        """旧数据（帧缺 detected_shot_* 元数据）时切点门不放行。"""
        shot = _shot(
            transition="硬切到特写",
            cut_transition_evidence_refs=["cut-transition-abc"],
        )
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["transition"] == ""

    def test_semantic_provider_path_unchanged(self):
        shot = _shot(
            transition="闪白转场",
            transition_evidence_refs=["transition-1"],
            analyzer_status={**_shot()["analyzer_status"], "transition": "analyzed"},
        )
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["transition"] == "闪白转场"
        assert _gate(out, "transition")["source"] == "semantic_provider"


class TestGateAnnotationsAndCompat:
    """标注契约与向后兼容。"""

    def test_every_shot_carries_evidence_gate(self):
        out = constrain_video_shots_to_evidence([_shot()])[0]
        for field in ("action", "camera", "transition"):
            entry = _gate(out, field)
            assert entry["verified"] is False
            assert entry["confidence"] is None

    def test_positional_single_argument_signature_still_works(self):
        assert constrain_video_shots_to_evidence(None) == []
        assert constrain_video_shots_to_evidence(["not-a-dict", 42]) == []

    def test_visual_and_lighting_cleaning_untouched(self):
        shot = _shot(visual="直接可见事实：产品居中。", lighting="未见")
        out = constrain_video_shots_to_evidence([shot])[0]
        assert out["visual"] == "产品居中"
        assert out["lighting"] == ""


class TestSubClauseUncertaintyDeletion:
    """清洗正则连坐删除修复：分句粒度删除不确定措辞。"""

    def test_comma_joined_valid_observation_survives(self):
        assert (
            clean_visual_generation_clause("主体为红色圆瓶居中，标签文字看不清")
            == "主体为红色圆瓶居中"
        )

    def test_dunhao_joined_clauses_filtered_individually(self):
        assert (
            clean_visual_generation_clause("画面明亮、疑似夜景、构图居中")
            == "画面明亮，构图居中"
        )

    def test_fully_uncertain_sentence_dropped_whole(self):
        assert clean_visual_generation_clause("疑似玻璃材质，可能为夜景") == ""

    def test_sentence_boundary_granularity_preserved(self):
        assert (
            clean_visual_generation_clause("瓶身为磨砂玻璃。品牌可能是进口的。柔和顶光")
            == "瓶身为磨砂玻璃。柔和顶光"
        )
