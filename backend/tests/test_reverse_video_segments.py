from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas import ReverseOperationCreate
from app.services import gateway_prompting, reverse_operations, video_audio, video_frames


def _body(**overrides):
    values = {
        "client_request_id": "video-segments-request-001",
        "asset_url": "https://cdn.example.com/source.mp4",
        "target": "video",
        "source_type": "video",
        **overrides,
    }
    return ReverseOperationCreate(**values)


def test_legacy_source_range_normalizes_to_canonical_ranges():
    body = _body(source_range={"start_seconds": 10, "end_seconds": 20})

    assert [item.model_dump() for item in body.source_ranges] == [
        {"start_seconds": 10.0, "end_seconds": 20.0}
    ]
    assert body.source_range is not None
    payload = reverse_operations._body_dict(body)
    assert payload["source_ranges"] == [{"start_seconds": 10.0, "end_seconds": 20.0}]


def test_source_ranges_sort_and_participate_in_fingerprint():
    body = _body(source_ranges=[
        {"start_seconds": 20, "end_seconds": 25},
        {"start_seconds": 2, "end_seconds": 8},
    ])
    other = _body(
        client_request_id="video-segments-request-002",
        source_ranges=[
            {"start_seconds": 2, "end_seconds": 8},
            {"start_seconds": 20, "end_seconds": 26},
        ],
    )

    assert [item.start_seconds for item in body.source_ranges] == [2, 20]
    assert body.source_range is None
    assert reverse_operations.request_fingerprint(body) != reverse_operations.request_fingerprint(other)


def test_overlapping_source_ranges_merge_to_one_canonical_range():
    body = _body(source_ranges=[
        {"start_seconds": 9, "end_seconds": 15},
        {"start_seconds": 0, "end_seconds": 10},
        {"start_seconds": 15, "end_seconds": 18},
    ])
    canonical = _body(
        client_request_id="video-segments-request-002",
        source_ranges=[{"start_seconds": 0, "end_seconds": 18}],
    )

    assert [item.model_dump() for item in body.source_ranges] == [
        {"start_seconds": 0.0, "end_seconds": 18.0}
    ]
    assert body.source_range is not None
    assert reverse_operations.request_fingerprint(body) == reverse_operations.request_fingerprint(canonical)


@pytest.mark.parametrize(
    "source_ranges,custom_keyframes,match",
    [
        (
            [{"start_seconds": 0, "end_seconds": 200}, {"start_seconds": 300, "end_seconds": 401}],
            [],
            "300 秒",
        ),
        (
            [{"start_seconds": 10, "end_seconds": 20}, {"start_seconds": 30, "end_seconds": 40}],
            [25],
            "已选分析片段",
        ),
    ],
)
def test_source_range_contract_rejects_invalid_selection(source_ranges, custom_keyframes, match):
    with pytest.raises(ValidationError, match=match):
        _body(source_ranges=source_ranges, custom_keyframes=custom_keyframes)


def test_sampler_merges_overlapping_ranges_before_frame_distribution(monkeypatch, tmp_path):
    monkeypatch.setattr(video_frames, "probe_media", lambda *_args: {
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "has_audio": False,
        "duration_seconds": 20,
    })
    monkeypatch.setattr(video_frames, "_scene_change_timestamps", lambda *_args: [])

    def fake_grab(_src, timestamp, destination):
        Path(destination).write_bytes(f"frame-{timestamp}".encode())
        return timestamp

    monkeypatch.setattr(video_frames, "_grab_frame_with_backoff", fake_grab)
    source = tmp_path / "overlap.mp4"
    source.write_bytes(b"video")

    sample = video_frames._sample_video_from_file(
        str(source),
        4,
        preset="fast",
        source_ranges=[
            {"start_seconds": 0, "end_seconds": 10},
            {"start_seconds": 8, "end_seconds": 15},
        ],
    )

    assert sample.source.source_ranges == ((0.0, 15.0),)
    assert {frame.source_segment_index for frame in sample.frames} == {1}


@pytest.mark.parametrize(
    "source_ranges,custom_timestamps,match",
    [
        ([{"start_seconds": 3, "end_seconds": 7}], [], "结束时间 7 秒超过素材时长"),
        ([{"start_seconds": 7, "end_seconds": 8}], [], "开始时间 7 秒超过素材时长"),
        ([{"start_seconds": 0, "end_seconds": 6.016}], [7], "关键帧时间 7 秒超过素材时长"),
    ],
)
def test_sampler_rejects_times_outside_authoritative_duration(
    monkeypatch,
    tmp_path,
    source_ranges,
    custom_timestamps,
    match,
):
    monkeypatch.setattr(video_frames, "probe_media", lambda *_args: {
        "width": 960,
        "height": 540,
        "fps": 24,
        "has_audio": False,
        "duration_seconds": 6.016,
    })
    source = tmp_path / "selection.mp4"
    source.write_bytes(b"video")

    with pytest.raises(video_frames.VideoSelectionError, match=match):
        video_frames._sample_video_from_file(
            str(source),
            4,
            preset="fast",
            source_ranges=source_ranges,
            custom_timestamps=custom_timestamps,
        )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg and ffprobe required",
)
def test_real_mp4_multi_segment_sampling_keeps_absolute_frame_evidence(tmp_path):
    source = tmp_path / "real-segments.mp4"
    subprocess.run(
        [
            str(shutil.which("ffmpeg")),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=24:duration=2.2",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
    )

    sample = video_frames._sample_video_from_file(
        str(source),
        6,
        preset="fine",
        source_ranges=[
            {"start_seconds": 0.1, "end_seconds": 0.7},
            {"start_seconds": 1.3, "end_seconds": 1.9},
        ],
        custom_timestamps=[0.4, 1.6],
    )

    assert sample.source.source_ranges == ((0.1, 0.7), (1.3, 1.9))
    assert {frame.source_segment_index for frame in sample.frames} == {1, 2}
    assert all(frame.jpeg.startswith(b"\xff\xd8") for frame in sample.frames)
    assert all(
        0.05 <= frame.timestamp_seconds <= 0.75
        if frame.source_segment_index == 1
        else 1.25 <= frame.timestamp_seconds <= 1.95
        for frame in sample.frames
    )
    analysis = sample.analysis()
    assert all(
        row["absolute_timestamp_seconds"] == row["timestamp_seconds"]
        for row in analysis["sampled_frames"]
    )


def test_multi_segment_sampler_distributes_frames_and_keeps_absolute_time(monkeypatch, tmp_path):
    monkeypatch.setattr(video_frames, "probe_media", lambda *_args: {
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "has_audio": True,
        "duration_seconds": 40,
    })
    monkeypatch.setattr(video_frames, "_scene_change_timestamps", lambda *_args: [3, 22])

    def fake_grab(_src, timestamp, destination):
        Path(destination).write_bytes(f"frame-{timestamp}".encode())
        return timestamp

    monkeypatch.setattr(video_frames, "_grab_frame_with_backoff", fake_grab)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    sample = video_frames._sample_video_from_file(
        str(source),
        4,
        preset="fast",
        source_ranges=[
            {"start_seconds": 2, "end_seconds": 8},
            {"start_seconds": 20, "end_seconds": 26},
        ],
        custom_timestamps=[4, 24],
    )

    assert len(sample.frames) == 4
    assert {frame.source_segment_index for frame in sample.frames} == {1, 2}
    assert all(2 <= frame.timestamp_seconds <= 8 for frame in sample.frames if frame.source_segment_index == 1)
    assert all(20 <= frame.timestamp_seconds <= 26 for frame in sample.frames if frame.source_segment_index == 2)
    analysis = sample.analysis()
    detection = analysis["shot_detection"]
    assert detection["status"] == "analyzed"
    assert detection["scene_boundary_count"] == 2
    assert [
        (row["source_segment_index"], row["start_seconds"], row["end_seconds"])
        for row in detection["shots"]
    ] == [
        (1, 2.0, 3.0),
        (1, 3.0, 8.0),
        (2, 20.0, 22.0),
        (2, 22.0, 26.0),
    ]
    assert all(row["boundary_refs"] for row in detection["shots"])
    assert analysis["source"]["duration_seconds"] == 12
    assert analysis["source"]["total_duration_seconds"] == 40
    assert analysis["source"]["source_ranges"][1]["start_seconds"] == 20
    assert analysis["sampled_frames"][0]["absolute_timestamp_seconds"] >= 2


def test_multi_segment_audio_merges_absolute_timestamped_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(video_audio.video_frames, "probe_media", lambda *_args: {"has_audio": True})
    monkeypatch.setattr(video_audio, "configured", lambda: True)
    monkeypatch.setattr(video_audio.settings, "audio_gateway_enabled", True)
    monkeypatch.setattr(video_audio, "_extract_audio", lambda *_args, **_kwargs: (True, None))

    def fake_transcribe(_path, *, offset_seconds, duration):
        return {
            "status": "analyzed",
            "transcript": f"segment {offset_seconds}",
            "segments": [{
                "start_seconds": offset_seconds + 0.5,
                "end_seconds": offset_seconds + min(1.5, duration),
                "relative_start_seconds": 0.5,
                "relative_end_seconds": min(1.5, duration),
                "text": f"line {offset_seconds}",
            }],
            "language": "zh",
            "provider_model": "test",
            "degraded_reason": None,
        }

    monkeypatch.setattr(video_audio, "_transcribe", fake_transcribe)
    result = video_audio.analyze_video_audio_ranges_from_path(
        str(tmp_path / "source.mp4"),
        source_ranges=[
            {"start_seconds": 10, "end_seconds": 15},
            {"start_seconds": 30, "end_seconds": 35},
        ],
    )

    assert result["status"] == "analyzed"
    assert result["complete"] is True
    assert result["selected_segment_count"] == 2
    assert [item["start_seconds"] for item in result["segments"]] == [10.5, 30.5]
    assert [item["source_segment_index"] for item in result["segments"]] == [1, 2]


def test_multi_segment_shots_and_gaps_use_absolute_source_time():
    ranges = [
        {"start_seconds": 10, "end_seconds": 15},
        {"start_seconds": 30, "end_seconds": 35},
    ]
    frames = [
        {"index": 1, "absolute_timestamp_seconds": 10.5, "source_segment_index": 1},
        {"index": 2, "absolute_timestamp_seconds": 12.5, "source_segment_index": 1},
        {"index": 3, "absolute_timestamp_seconds": 30.5, "source_segment_index": 2},
        {"index": 4, "absolute_timestamp_seconds": 32.5, "source_segment_index": 2},
    ]
    shots = gateway_prompting.normalize_video_shots(
        [
            {"source_segment_index": 1, "start_seconds": 10, "end_seconds": 13, "visual": "A", "evidence_frame_indices": [1, 2], "confidence": 0.9},
            {"source_segment_index": 2, "start_seconds": 30, "end_seconds": 33, "visual": "B", "evidence_frame_indices": [3, 4], "confidence": 0.9},
        ],
        duration_seconds=10,
        frame_count=4,
        sampled_frames=frames,
        source_ranges=ranges,
    )

    assert [(item["source_segment_index"], item["start_seconds"]) for item in shots] == [(1, 10.0), (2, 30.0)]
    gaps = gateway_prompting.video_analysis_gaps(
        shots,
        duration_seconds=10,
        source_ranges=ranges,
        analysis_mode="keyframes_multi_segment",
    )
    assert gaps == [
        {"source_segment_index": 1, "start_seconds": 13.0, "end_seconds": 15.0},
        {"source_segment_index": 2, "start_seconds": 33.0, "end_seconds": 35.0},
    ]
