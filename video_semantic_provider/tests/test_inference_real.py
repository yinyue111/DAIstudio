"""真实推理纯函数与引擎逻辑的单测（不依赖模型权重）。

test_contract.py 走 HTTP 层验证契约；这里直接针对 bbox 归一化、IoU、
姿态-主体框匹配等纯函数，以及真实 OpenCvSemanticEngine 在合成帧上的
跟踪 / 转场输出做断言，保证这些真实逻辑在 CI 里被执行。
"""
from __future__ import annotations

from typing import Any

import numpy as np

from video_semantic_provider.app.config import ProviderSettings
from video_semantic_provider.app.inference import (
    OpenCvSemanticEngine,
    _bbox,
    _iou,
    _pose_matches_bbox,
)


def test_bbox_normalizes_and_clamps_to_frame_bounds() -> None:
    # 负坐标被钳到 0，越界宽高被裁剪到帧内，保留 6 位小数。
    assert _bbox((-10, 16, 200, 32), width=100, height=64) == {
        "x": 0.0,
        "y": 0.25,
        "width": 1.0,
        "height": 0.5,
    }
    assert _bbox((25, 8, 50, 16), width=100, height=64) == {
        "x": 0.25,
        "y": 0.125,
        "width": 0.5,
        "height": 0.25,
    }
    third = _bbox((1, 1, 1, 1), width=3, height=3)
    assert third == {
        "x": 0.333333,
        "y": 0.333333,
        "width": 0.333333,
        "height": 0.333333,
    }


def test_iou_matches_hand_computed_values() -> None:
    assert _iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert _iou((0, 0, 10, 10), (20, 20, 10, 10)) == 0.0
    # 交集 25，并集 175 -> 1/7
    assert abs(_iou((0, 0, 10, 10), (5, 5, 10, 10)) - 25 / 175) < 1e-9
    # 零面积退化框不允许除零
    assert _iou((0, 0, 0, 0), (0, 0, 0, 0)) == 0.0


def test_pose_matches_bbox_requires_majority_inside_with_margin() -> None:
    bbox = {"x": 0.4, "y": 0.4, "width": 0.2, "height": 0.2}
    inside = {"x": 0.5, "y": 0.5}
    margin_edge = {"x": 0.26, "y": 0.5}  # 落在 0.15 容差带内
    outside = {"x": 0.05, "y": 0.05}

    assert _pose_matches_bbox([inside, margin_edge], bbox) is True
    assert _pose_matches_bbox([inside, outside, outside], bbox) is False
    # 恰好一半在框内（含容差）也算匹配：inside*2 >= len
    assert _pose_matches_bbox([inside, outside], bbox) is True
    assert _pose_matches_bbox([], bbox) is False


def _frame(
    index: int,
    timestamp: float,
    image: np.ndarray,
    *,
    segment: int = 1,
) -> dict[str, Any]:
    return {
        "frame_index": index,
        "absolute_timestamp_seconds": timestamp,
        "source_segment_index": segment,
        "image": image,
    }


def _blank(width: int = 96, height: int = 64) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


def _with_square(offset: int, *, size: int = 18) -> np.ndarray:
    image = _blank()
    image[20 : 20 + size, offset : offset + size] = 255
    return image


def test_real_engine_tracks_moving_square_with_normalized_bboxes() -> None:
    engine = OpenCvSemanticEngine(ProviderSettings())
    frames = [
        _frame(1, 0.0, _with_square(10)),
        _frame(2, 1.0, _with_square(18)),
        _frame(3, 2.0, _with_square(26)),
        _frame(4, 3.0, _with_square(34)),
    ]

    result = engine.analyze(frames)

    assert result["contract_version"] == "video-semantic-evidence.v1"
    tracking = result["capabilities"]["subject_tracking"]
    assert tracking["status"] == "analyzed"
    assert tracking["evidence"], "移动方块必须产生至少一条主体轨迹"
    track = tracking["evidence"][0]
    assert track["label"] == "moving_subject"
    assert track["source_segment_index"] == 1
    assert len(track["observations"]) >= 2
    assert track["start_seconds"] <= track["end_seconds"]
    for observation in track["observations"]:
        bbox = observation["bbox"]
        # bbox 已归一化到 [0,1] 并且不携带像素坐标
        assert "pixel_bbox" not in observation
        for key in ("x", "y", "width", "height"):
            assert 0.0 <= bbox[key] <= 1.0, f"bbox.{key} 必须归一化到 [0,1]"
        assert bbox["x"] + bbox["width"] <= 1.0 + 1e-6
        assert bbox["y"] + bbox["height"] <= 1.0 + 1e-6
        assert 0.25 <= observation["confidence"] <= 0.95
    # 动作识别必须诚实报告不支持，不产出伪证据。
    assert result["capabilities"]["action"]["status"] == "unsupported"


def test_real_engine_reports_partial_when_frames_are_static() -> None:
    engine = OpenCvSemanticEngine(ProviderSettings())
    static = _with_square(10)
    frames = [_frame(i, float(i), static.copy()) for i in range(1, 4)]

    result = engine.analyze(frames)

    tracking = result["capabilities"]["subject_tracking"]
    assert tracking["status"] == "partial"
    assert tracking["evidence"] == []
    assert tracking["degraded_reason"]


def test_real_engine_detects_abrupt_transition_between_contrasting_frames() -> None:
    engine = OpenCvSemanticEngine(ProviderSettings())
    dark = _blank()
    bright = np.full((64, 96, 3), 255, dtype=np.uint8)
    frames = [
        _frame(1, 1.0, dark),
        _frame(2, 2.0, bright),
    ]

    result = engine.analyze(frames)

    transition = result["capabilities"]["transition"]
    assert transition["status"] == "analyzed"
    assert len(transition["evidence"]) == 1
    row = transition["evidence"][0]
    assert row["label"] == "visual_discontinuity"
    assert row["before_frame_index"] == 1
    assert row["after_frame_index"] == 2
    # 转场时间戳取相邻两帧中点
    assert row["timestamp_seconds"] == 1.5
    assert 0.0 <= row["confidence"] <= 0.99


def test_real_engine_keeps_segments_isolated_when_tracking() -> None:
    engine = OpenCvSemanticEngine(ProviderSettings())
    frames = [
        _frame(1, 0.0, _with_square(10), segment=1),
        _frame(2, 1.0, _with_square(20), segment=1),
        _frame(3, 2.0, _with_square(30), segment=1),
        _frame(1, 0.0, _with_square(60), segment=2),
        _frame(2, 1.0, _with_square(50), segment=2),
        _frame(3, 2.0, _with_square(40), segment=2),
    ]

    result = engine.analyze(frames)

    tracking = result["capabilities"]["subject_tracking"]
    segments = {row["source_segment_index"] for row in tracking["evidence"]}
    assert segments == {1, 2}, "跨分段的运动不允许合并成同一条轨迹"
    for row in tracking["evidence"]:
        assert str(row["evidence_id"]).startswith(
            f"motion-subject-{row['source_segment_index']}-"
        )
