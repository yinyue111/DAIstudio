"""反推视频证据信号：运镜标签/切点排除/切点转场证据/切点优先取帧。

覆盖四条链路：
1. 光流样本必须带可判别的运镜标签 + 方向 + 置信度（下游做标签级对账）；
2. 跨 ffmpeg 切点的帧对不得进入光流样本（剪辑跳变不是运镜）；
3. ffmpeg 场景切点整理成规范的 transition 证据并挂到镜头上；
4. 帧选择让切点优先于均匀锚点，同时保底长镜头覆盖。
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app.services import video_evidence_analysis, video_frames


def _data_uri(image: Image.Image) -> str:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def _feature_image():
    import cv2
    import numpy as np

    rng = np.random.default_rng(20260726)
    base = np.zeros((180, 240, 3), dtype=np.uint8)
    for y in range(15, 170, 25):
        for x in range(15, 230, 25):
            color = tuple(int(value) for value in rng.integers(80, 256, size=3))
            cv2.circle(base, (x, y), 5, color, -1)
            cv2.rectangle(base, (x - 7, y - 7), (x + 7, y + 7), color, 1)
    return base


def _frame_meta(index: int, timestamp: float, *, shot: int | None = None) -> dict:
    meta = {
        "frame_index": index,
        "timestamp_seconds": timestamp,
        "source_segment_index": 1,
    }
    if shot is not None:
        meta["detected_shot_index"] = shot
    return meta


def _video_ocr_unsupported(_image):
    return {
        "status": "unsupported", "analyzer": "fixture_ocr", "analyzer_version": "none",
        "degraded_reason": "fixture unavailable", "evidence": [],
    }


def _motion_unsupported(_images, _metadata):
    return {
        "status": "unsupported", "analyzer": "fixture_motion",
        "analyzer_version": "none", "samples": [],
        "degraded_reason": "fixture unavailable",
    }


def _semantic_unsupported(_images, _metadata):
    return video_evidence_analysis._semantic_failure("unsupported", "fixture unavailable")


# ---------------------------------------------------------------------------
# 1. 光流样本的运镜标签契约
# ---------------------------------------------------------------------------

def test_cv2_motion_samples_expose_labelled_scores_and_direction():
    import cv2
    import numpy as np

    base = _feature_image()
    variants = {
        # 内容右移（8px，小于纹理周期一半避免光流混叠）=> 相机左摇
        ("pan", "left"): cv2.warpAffine(
            base, np.float32([[1, 0, 8], [0, 1, 0]]), (240, 180),
        ),
        # 画面放大 => 推近
        ("zoom", "in"): cv2.warpAffine(
            base, cv2.getRotationMatrix2D((120, 90), 0, 1.12), (240, 180),
        ),
        ("static", None): base.copy(),
    }
    for (expected_label, expected_direction), transformed in variants.items():
        result = video_evidence_analysis.cv2_motion_analysis(
            [Image.fromarray(base), Image.fromarray(transformed)],
            [_frame_meta(1, 1.0, shot=1), _frame_meta(2, 2.0, shot=1)],
        )
        assert result["status"] == "analyzed"
        assert result["camera_labels"] == list(video_evidence_analysis.CAMERA_MOTION_LABELS)
        assert result["excluded_cross_cut_pairs"] == 0
        sample = result["samples"][0]
        assert sample["camera"] == expected_label
        assert sample["camera_direction"] == expected_direction
        assert set(sample["camera_scores"]) == set(video_evidence_analysis.CAMERA_MOTION_LABELS)
        assert sample["camera_confidence"] == sample["camera_scores"][expected_label]
        assert 0.0 <= sample["camera_confidence"] <= 1.0
        if expected_direction is not None:
            assert expected_direction in (
                video_evidence_analysis.CAMERA_MOTION_DIRECTIONS[expected_label]
            )


# ---------------------------------------------------------------------------
# 2. 跨切点帧对排除
# ---------------------------------------------------------------------------

def test_cv2_motion_excludes_cross_cut_frame_pairs():
    import cv2
    import numpy as np

    base = _feature_image()
    panned = cv2.warpAffine(base, np.float32([[1, 0, 18], [0, 1, 0]]), (240, 180))
    other_scene = np.ascontiguousarray(base[::-1, ::-1])
    result = video_evidence_analysis.cv2_motion_analysis(
        [Image.fromarray(base), Image.fromarray(panned), Image.fromarray(other_scene)],
        [
            _frame_meta(1, 1.0, shot=1),
            _frame_meta(2, 2.0, shot=1),
            _frame_meta(3, 4.0, shot=2),
        ],
    )

    assert result["status"] == "analyzed"
    assert result["excluded_cross_cut_pairs"] == 1
    assert [sample["frame_indices"] for sample in result["samples"]] == [[1, 2]]


def test_cv2_motion_degrades_honestly_when_every_pair_crosses_a_cut():
    base = _feature_image()
    result = video_evidence_analysis.cv2_motion_analysis(
        [Image.fromarray(base), Image.fromarray(base.copy())],
        [_frame_meta(1, 1.0, shot=1), _frame_meta(2, 2.0, shot=2)],
    )

    assert result["status"] == "degraded"
    assert result["samples"] == []
    assert result["excluded_cross_cut_pairs"] == 1
    assert "切点" in result["degraded_reason"]


def test_cv2_motion_keeps_pairs_when_shot_metadata_is_missing():
    """旧元数据没有镜头归属时不能误伤：行为退回到只按片段过滤。"""
    import cv2
    import numpy as np

    base = _feature_image()
    panned = cv2.warpAffine(base, np.float32([[1, 0, 18], [0, 1, 0]]), (240, 180))
    result = video_evidence_analysis.cv2_motion_analysis(
        [Image.fromarray(base), Image.fromarray(panned)],
        [_frame_meta(1, 1.0), _frame_meta(2, 2.0)],
    )

    assert result["status"] == "analyzed"
    assert result["excluded_cross_cut_pairs"] == 0
    assert len(result["samples"]) == 1


# ---------------------------------------------------------------------------
# 3. 切点转场证据 + 镜头挂载（含运镜摘要）
# ---------------------------------------------------------------------------

def _sampled_rows_with_shots() -> list[dict]:
    return [
        {
            "index": 1, "absolute_timestamp_seconds": 2.0, "source_segment_index": 1,
            "detected_shot_index": 1, "detected_shot_id": "detected-shot-1-0-3500",
            "detected_shot_start_seconds": 0.0, "detected_shot_cut_score": None,
        },
        {
            "index": 2, "absolute_timestamp_seconds": 5.0, "source_segment_index": 1,
            "detected_shot_index": 2, "detected_shot_id": "detected-shot-1-3500-8000",
            "detected_shot_start_seconds": 3.5, "detected_shot_cut_score": 0.62,
        },
    ]


def test_analyze_video_evidence_emits_cut_transition_events():
    image = _data_uri(Image.new("RGB", (20, 20), "white"))
    result = video_evidence_analysis.analyze_video_evidence(
        [image, image],
        _sampled_rows_with_shots(),
        ocr_analyzer=_video_ocr_unsupported,
        motion_analyzer=_motion_unsupported,
        semantic_analyzer=_semantic_unsupported,
    )

    block = result["shot_transitions"]
    assert block["status"] == "analyzed"
    assert block["analyzer"] == "ffmpeg_scene"
    assert block["evidence_count"] == 1
    event = block["events"][0]
    assert event["label"] == "hard_cut"
    assert event["timestamp_seconds"] == 3.5
    assert event["before_frame_index"] == 1
    assert event["after_frame_index"] == 2
    assert event["before_timestamp_seconds"] == 2.0
    assert event["after_timestamp_seconds"] == 5.0
    assert event["cut_count"] == 1
    assert event["confidence"] == 0.62
    assert event["confidence_source"] == "ffmpeg_scene_score"
    assert event["evidence_id"].startswith("cut-transition-")
    assert len(event["source_content_hashes"]) == 2
    assert all(event["source_content_hashes"])
    # 硬切证据独立存在，不污染 http 语义 provider 的 transition 能力位
    assert result["transition"]["status"] == "unsupported"


def test_cut_transition_confidence_falls_back_when_scene_score_missing():
    rows = _sampled_rows_with_shots()
    rows[1]["detected_shot_cut_score"] = None
    block = video_evidence_analysis.build_cut_transition_evidence([
        {
            "frame_index": row["index"],
            "timestamp_seconds": row["absolute_timestamp_seconds"],
            "source_segment_index": row["source_segment_index"],
            "detected_shot_index": row["detected_shot_index"],
            "detected_shot_start_seconds": row["detected_shot_start_seconds"],
            "detected_shot_cut_score": row["detected_shot_cut_score"],
            "source_content_hash": f"hash-{row['index']}",
        }
        for row in rows
    ])

    assert block["status"] == "analyzed"
    event = block["events"][0]
    assert event["confidence"] == video_evidence_analysis.DEFAULT_CUT_TRANSITION_CONFIDENCE
    assert event["confidence_source"] == "default_hard_cut"


def test_cut_transitions_unsupported_without_shot_metadata():
    image = _data_uri(Image.new("RGB", (20, 20), "white"))
    result = video_evidence_analysis.analyze_video_evidence(
        [image, image],
        [
            {"index": 1, "absolute_timestamp_seconds": 2.0, "source_segment_index": 1},
            {"index": 2, "absolute_timestamp_seconds": 5.0, "source_segment_index": 1},
        ],
        ocr_analyzer=_video_ocr_unsupported,
        motion_analyzer=_motion_unsupported,
        semantic_analyzer=_semantic_unsupported,
    )

    block = result["shot_transitions"]
    assert block["status"] == "unsupported"
    assert block["events"] == []
    assert "镜头切点元数据" in block["degraded_reason"]

    shot = video_evidence_analysis.attach_evidence_to_shots(
        [{"start_seconds": 0.0, "end_seconds": 6.0, "source_segment_index": 1}],
        result,
    )[0]
    assert shot["cut_transition_evidence_refs"] == []
    assert shot["analyzer_status"]["shot_transitions"] == "unsupported"


def test_shot_attachment_carries_cut_transition_refs_and_camera_summary():
    image = _data_uri(Image.new("RGB", (20, 20), "white"))
    camera_motion = {
        "status": "analyzed",
        "analyzer": "opencv_lk_homography",
        "analyzer_version": "4.9",
        "camera_labels": list(video_evidence_analysis.CAMERA_MOTION_LABELS),
        "samples": [
            {
                "evidence_id": "motion-fixture-a",
                "start_seconds": 3.6, "end_seconds": 4.2, "source_segment_index": 1,
                "frame_indices": [2, 3], "camera": "pan", "camera_direction": "left",
                "camera_confidence": 0.8,
                "camera_scores": {"pan": 0.8, "tilt": 0.1, "zoom": 0.1, "static": 0.0},
                "background_motion_confidence": 0.9, "subject_motion_confidence": 0.1,
            },
            {
                "evidence_id": "motion-fixture-b",
                "start_seconds": 4.2, "end_seconds": 5.0, "source_segment_index": 1,
                "frame_indices": [3, 4], "camera": "pan", "camera_direction": "left",
                "camera_confidence": 0.6,
                "camera_scores": {"pan": 0.6, "tilt": 0.2, "zoom": 0.1, "static": 0.1},
                "background_motion_confidence": 0.9, "subject_motion_confidence": 0.1,
            },
        ],
        "excluded_cross_cut_pairs": 0,
        "degraded_reason": None,
    }
    result = video_evidence_analysis.analyze_video_evidence(
        [image, image],
        _sampled_rows_with_shots(),
        ocr_analyzer=_video_ocr_unsupported,
        motion_analyzer=lambda _images, _metadata: camera_motion,
        semantic_analyzer=_semantic_unsupported,
    )
    cut_event_id = result["shot_transitions"]["events"][0]["evidence_id"]

    covering, outside = video_evidence_analysis.attach_evidence_to_shots(
        [
            {"start_seconds": 3.0, "end_seconds": 6.0, "source_segment_index": 1},
            {"start_seconds": 0.0, "end_seconds": 3.0, "source_segment_index": 1},
        ],
        result,
    )

    assert covering["cut_transition_evidence_refs"] == [cut_event_id]
    assert covering["analyzer_status"]["shot_transitions"] == "analyzed"
    summary = covering["camera_motion_summary"]
    assert summary["status"] == "analyzed"
    assert summary["dominant_label"] == "pan"
    assert summary["dominant_direction"] == "left"
    assert summary["label_scores"] == {"pan": 0.7, "tilt": 0.15, "zoom": 0.1, "static": 0.05}
    assert summary["confidence"] == 0.7
    assert summary["sample_count"] == 2
    assert summary["evidence_refs"] == ["motion-fixture-a", "motion-fixture-b"]
    assert summary["evidence_refs"] == covering["camera_motion_evidence_refs"]

    # 切点 3.5 不在 [0,3] 内；该镜头也没有光流样本
    assert outside["cut_transition_evidence_refs"] == []
    assert outside["camera_motion_summary"]["status"] == "no_evidence"
    assert outside["camera_motion_summary"]["dominant_label"] is None


# ---------------------------------------------------------------------------
# 4. 帧选择：切点真正优先 + 长镜头保底覆盖
# ---------------------------------------------------------------------------

def test_merge_timestamps_eats_every_cut_in_a_short_fast_cut_video():
    merged = video_frames._merge_timestamps(
        [2.0, 3.5, 9.0, 16.0],
        video_frames._uniform_timestamps(20.0, 6),
        6,
        20.0,
    )

    assert len(merged) == 6
    assert merged[0] == 0.0
    assert merged[-1] == 19.95
    # 四个切点全部入选，而不是每个均匀桶最多吸附一个
    assert {2.0, 3.5, 9.0, 16.0}.issubset(set(merged))


def test_merge_timestamps_keeps_long_shot_coverage_when_cuts_cluster_early():
    cuts = [2.0, 4.0, 8.0, 12.0, 18.0, 24.0, 30.0]
    merged = video_frames._merge_timestamps(
        cuts,
        video_frames._uniform_timestamps(300.0, 8),
        8,
        300.0,
    )

    assert len(merged) == 8
    assert merged[0] == 0.0
    assert merged[-1] == 299.95
    # 至少吃进两个切点（旧算法只有一个桶能吸附到）
    assert len(set(merged) & set(cuts)) >= 2
    # 尾部长镜头仍有均匀回填，空窗不失控
    assert max(right - left for left, right in zip(merged, merged[1:])) <= 70


def test_merge_timestamps_keeps_user_requested_keyframes_first():
    merged = video_frames._merge_timestamps(
        [],
        video_frames._uniform_timestamps(20.0, 5),
        5,
        20.0,
        requested=[11.3],
    )

    assert len(merged) == 5
    assert 11.3 in merged


def test_scene_change_timestamps_carry_lavfi_scene_scores(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    stdout = "\n".join([
        "frame:0 pts:1024 pts_time:1.0",
        "lavfi.scene_score=0.415",
        "frame:1 pts:2048 pts_time:1.2",
        "lavfi.scene_score=0.9",
        "frame:2 pts:3072 pts_time:4.0",
        "lavfi.scene_score=0.62",
    ])
    monkeypatch.setattr(
        video_frames.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=stdout)
    )

    stamps = video_frames._scene_change_timestamps("/tmp/video.mp4", limit=8)

    # 对旧消费方仍是 float；1.2 因最小帧距被合并掉
    assert stamps == [1.0, 4.0]
    assert stamps[0].scene_score == 0.415
    assert stamps[1].scene_score == 0.62


def test_sampled_frames_carry_detected_shot_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(video_frames, "probe_media", lambda *_args: {
        "width": 1280, "height": 720, "fps": 25,
        "has_audio": False, "duration_seconds": 20,
    })
    monkeypatch.setattr(
        video_frames,
        "_scene_change_timestamps",
        lambda *_args, **_kwargs: [video_frames._SceneCut(3.0, 0.44)],
    )

    def fake_grab(_src, timestamp, destination):
        Path(destination).write_bytes(f"frame-{timestamp}".encode())
        return timestamp

    monkeypatch.setattr(video_frames, "_grab_frame_with_backoff", fake_grab)
    source = tmp_path / "cuts.mp4"
    source.write_bytes(b"video")

    sample = video_frames._sample_video_from_file(str(source), 4)

    analysis = sample.analysis()
    detection = analysis["shot_detection"]
    scene_boundaries = [
        row for row in detection["boundaries"] if row["boundary_type"] == "scene_change"
    ]
    assert [row["scene_score"] for row in scene_boundaries] == [0.44]
    assert [row.get("start_scene_score") for row in detection["shots"]] == [None, 0.44]

    rows = analysis["sampled_frames"]
    assert 3.0 in [row["timestamp_seconds"] for row in rows]
    assert [row["detected_shot_index"] for row in rows] == [1, 2, 2, 2]
    cut_frame = next(row for row in rows if row["timestamp_seconds"] == 3.0)
    # 恰好落在切点上的帧归属后一个镜头，并携带该切点的置信度
    assert cut_frame["detected_shot_index"] == 2
    assert cut_frame["detected_shot_start_seconds"] == 3.0
    assert cut_frame["detected_shot_cut_score"] == 0.44
    first_frame = next(row for row in rows if row["timestamp_seconds"] == 0.0)
    assert first_frame["detected_shot_index"] == 1
    assert first_frame["detected_shot_cut_score"] is None
