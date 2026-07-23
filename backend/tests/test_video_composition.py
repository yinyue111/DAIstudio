from __future__ import annotations

import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import SessionLocal
from app.models import GenAsset, GenTask, UploadedAsset
from app.services import storage, user_assets, video_composition, video_frames


def _uploaded_video(db, *, user_id: int, payload: bytes, filename: str) -> str:
    key = storage.save_bytes(payload, "upload_video", "mp4")
    db.add(
        UploadedAsset(
            key=key,
            user_id=user_id,
            mime="video/mp4",
            width=64,
            height=64,
            duration=1,
            bytes=len(payload),
            original_filename=filename,
        )
    )
    db.commit()
    return user_assets.uploaded_asset_ref(key)


def _generated_video(db, *, user_id: int, payload: bytes) -> tuple[str, int]:
    key = storage.save_bytes(payload, "video_hd", "mp4")
    task = GenTask(
        user_id=user_id,
        category="video",
        stage="final",
        status="succeeded",
        cost_frozen=0,
        cost_settled=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(task)
    db.flush()
    asset = GenAsset(
        task_id=task.id,
        user_id=user_id,
        type="video",
        preview_url=storage.public_url(key),
        hd_url=storage.public_url(key),
        watermarked=False,
        unlocked=True,
        moderation_status="active",
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return user_assets.generated_asset_ref(asset.id), int(task.id)


def _audio_video_bytes() -> bytes:
    with tempfile.TemporaryDirectory() as temp_dir:
        output = Path(temp_dir) / "audio.mp4"
        subprocess.run(
            [
                str(shutil.which("ffmpeg")),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=64x64:d=0.6:r=24",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.6:sample_rate=48000",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-y",
                str(output),
            ],
            check=True,
            capture_output=True,
        )
        return output.read_bytes()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_video_composition_renders_subtitles_persists_asset_and_replays(
    client,
    make_user,
    tiny_mp4,
):
    user_id = make_user("13900004001")
    db = SessionLocal()
    try:
        first = _uploaded_video(db, user_id=user_id, payload=tiny_mp4, filename="first.mp4")
        second, second_generation_task_id = _generated_video(
            db,
            user_id=user_id,
            payload=tiny_mp4,
        )
        payload = {
            "schema_version": "video-composition.v1",
            "title": "Storyboard demo",
            "canvas": {"width": 64, "height": 64, "fps": 24, "background_color": "#101010"},
            "shots": [
                {
                    "shot_id": "shot-1",
                    "asset_ref": first,
                    "source_start_seconds": 0,
                    "source_end_seconds": 0.18,
                    "duration_seconds": 0.18,
                    "transition": {"type": "cut", "duration_seconds": 0},
                },
                {
                    "shot_id": "shot-2",
                    "asset_ref": second,
                    "source_start_seconds": 0,
                    "source_end_seconds": 0.18,
                    "duration_seconds": 0.18,
                    "transition": {"type": "cut", "duration_seconds": 0},
                },
            ],
            "subtitles": [
                {
                    "start_seconds": 0,
                    "end_seconds": 0.3,
                    "text": "TEST",
                    "font_size": 18,
                    "bottom_margin": 2,
                }
            ],
        }
        result = video_composition.compose_video(
            db,
            user_id=user_id,
            payload=payload,
            client_request_id="video-composition-test-001",
        )
        assert result["schema_version"] == "video-composition-result.v1"
        assert result["idempotent_replay"] is False
        assert result["manifest"]["shots"][0]["shot_id"] == "shot-1"
        assert result["manifest"]["shots"][0]["generation_task_id"] is None
        assert result["manifest"]["shots"][1]["generation_task_id"] == second_generation_task_id
        assert result["manifest"]["subtitles"][0]["text"] == "TEST"

        task = db.get(GenTask, result["task_id"])
        asset = db.get(GenAsset, result["asset_id"])
        assert task is not None and task.status == "succeeded" and task.cost_settled == 0
        assert asset is not None and asset.user_id == user_id and asset.unlocked is True
        output_key = storage.key_from_url(asset.hd_url)
        assert output_key and storage.exists(output_key)
        metadata = video_frames.probe_media(str(storage.local_path(output_key)))
        assert metadata["width"] == 64
        assert metadata["height"] == 64
        assert 0.2 <= float(metadata["duration"]) <= 1

        replay = video_composition.compose_video(
            db,
            user_id=user_id,
            payload=payload,
            client_request_id="video-composition-test-001",
        )
        assert replay["idempotent_replay"] is True
        assert replay["task_id"] == result["task_id"]
        assert replay["asset_id"] == result["asset_id"]
        assert replay["manifest"]["shots"][1]["generation_task_id"] == second_generation_task_id
    finally:
        db.close()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_video_composition_rejects_idempotency_key_reuse_with_different_input(
    client,
    make_user,
    tiny_mp4,
):
    user_id = make_user("13900004008")
    db = SessionLocal()
    try:
        source = _uploaded_video(db, user_id=user_id, payload=tiny_mp4, filename="source.mp4")
        payload = {
            "schema_version": "video-composition.v1",
            "title": "original",
            "canvas": {"width": 64, "height": 64, "fps": 24},
            "shots": [{"shot_id": "shot-1", "asset_ref": source, "duration_seconds": 0.15}],
        }
        video_composition.compose_video(
            db,
            user_id=user_id,
            payload=payload,
            client_request_id="video-composition-conflict-001",
        )

        with pytest.raises(video_composition.VideoCompositionError) as captured:
            video_composition.compose_video(
                db,
                user_id=user_id,
                payload={**payload, "title": "changed"},
                client_request_id="video-composition-conflict-001",
            )
        assert captured.value.code == "VIDEO_COMPOSITION_IDEMPOTENCY_CONFLICT"
    finally:
        db.close()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_video_composition_replays_persisted_manifest_without_ffmpeg(
    client,
    make_user,
    tiny_mp4,
    monkeypatch,
):
    user_id = make_user("13900004009")
    db = SessionLocal()
    try:
        source = _uploaded_video(db, user_id=user_id, payload=tiny_mp4, filename="source.mp4")
        payload = {
            "schema_version": "video-composition.v1",
            "title": "recoverable",
            "canvas": {"width": 64, "height": 64, "fps": 24},
            "shots": [{"shot_id": "shot-1", "asset_ref": source, "duration_seconds": 0.15}],
        }
        rendered = video_composition.compose_video(
            db,
            user_id=user_id,
            payload=payload,
            client_request_id="video-composition-recovery-001",
        )
        monkeypatch.setattr(video_composition, "FFMPEG", None)
        monkeypatch.setattr(video_frames, "FFPROBE", None)

        replay = video_composition.compose_video(
            db,
            user_id=user_id,
            payload=payload,
            client_request_id="video-composition-recovery-001",
        )
        assert replay["idempotent_replay"] is True
        assert replay["task_id"] == rendered["task_id"]
        assert replay["manifest"] == rendered["manifest"]
    finally:
        db.close()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_video_composition_rejects_forged_generation_task_lineage(
    client,
    make_user,
    tiny_mp4,
):
    user_id = make_user("13900004007")
    db = SessionLocal()
    try:
        generated_ref, generation_task_id = _generated_video(
            db,
            user_id=user_id,
            payload=tiny_mp4,
        )
        with pytest.raises(video_composition.VideoCompositionError) as captured:
            video_composition.compose_video(
                db,
                user_id=user_id,
                payload={
                    "schema_version": "video-composition.v1",
                    "shots": [{
                        "shot_id": "shot-forged-lineage",
                        "asset_ref": generated_ref,
                        "generation_task_id": generation_task_id + 1,
                        "duration_seconds": 0.15,
                    }],
                },
                client_request_id="video-composition-lineage-test-001",
            )
        assert captured.value.code == "VIDEO_COMPOSITION_LINEAGE_MISMATCH"
    finally:
        db.close()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_video_composition_rejects_cross_owner_assets(client, make_user, tiny_mp4):
    owner_id = make_user("13900004002")
    other_id = make_user("13900004003")
    db = SessionLocal()
    try:
        private_ref = _uploaded_video(
            db,
            user_id=owner_id,
            payload=tiny_mp4,
            filename="private.mp4",
        )
        with pytest.raises(video_composition.VideoCompositionError) as captured:
            video_composition.compose_video(
                db,
                user_id=other_id,
                payload={
                    "schema_version": "video-composition.v1",
                    "title": "forbidden",
                    "canvas": {"width": 64, "height": 64, "fps": 24},
                    "shots": [
                        {
                            "shot_id": "shot-private",
                            "asset_ref": private_ref,
                            "duration_seconds": 0.15,
                        }
                    ],
                },
                client_request_id="video-composition-owner-test-001",
            )
        assert captured.value.code == "VIDEO_COMPOSITION_ASSET_NOT_FOUND"
        task = db.query(GenTask).filter(
            GenTask.user_id == other_id,
            GenTask.client_request_id == "video-composition-owner-test-001",
        ).one()
        assert task.status == "failed"
    finally:
        db.close()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_video_composition_applies_crossfade_and_mixes_audio(client, make_user):
    user_id = make_user("13900004004")
    db = SessionLocal()
    try:
        source = _uploaded_video(
            db,
            user_id=user_id,
            payload=_audio_video_bytes(),
            filename="audio-source.mp4",
        )
        result = video_composition.compose_video(
            db,
            user_id=user_id,
            payload={
                "schema_version": "video-composition.v1",
                "title": "transition-and-audio",
                "canvas": {"width": 64, "height": 64, "fps": 24},
                "shots": [
                    {
                        "shot_id": "shot-a",
                        "asset_ref": source,
                        "duration_seconds": 0.3,
                        "transition": {"type": "crossfade", "duration_seconds": 0.1},
                    },
                    {
                        "shot_id": "shot-b",
                        "asset_ref": source,
                        "duration_seconds": 0.3,
                    },
                ],
                "audio_tracks": [
                    {
                        "asset_ref": source,
                        "start_seconds": 0,
                        "volume": 0.2,
                        "loop": True,
                    }
                ],
                "original_audio_volume": 0.5,
            },
            client_request_id="video-composition-audio-test-001",
        )
        asset = db.get(GenAsset, result["asset_id"])
        output_key = storage.key_from_url(asset.hd_url)
        metadata = video_frames.probe_media(str(storage.local_path(output_key)))
        assert metadata["has_audio"] is True
        assert 0.35 <= float(metadata["duration"]) <= 0.8
        assert result["manifest"]["audio_tracks"][0]["volume"] == 0.2
    finally:
        db.close()
