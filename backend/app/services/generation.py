"""Generation orchestration (runs inside Celery workers).

Image: gateway returns N images -> we save HD + a watermarked low-res preview
per image, settle the frozen estimate against real cost.
Video: submit async job -> poll to completion -> save preview (and HD for final
stage). Idempotent: a task that already reached a terminal state is skipped.
Failures refund the frozen credits only when the provider state is known. Video
submits with an unknown upstream state are held for admin reconciliation.
"""
from __future__ import annotations

import inspect
import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select, update

from ..config import settings
from ..db import SessionLocal
from ..models import GenAsset, GenTask
from ..redis_client import redis_client
from . import asset_refs, credits, gateway, locks, storage, usage, video_frames
from .config_store import get_model_config, get_setting
from .image_options import IMAGE_SIZES
from .model_gateway_config import (
    RuntimeGatewayConfig,
    gateway_key_fingerprint,
    runtime_config_for_model,
)
from .progress import set_progress
from .watermark import image_ext, make_image_preview, make_model_reference

log = logging.getLogger("generation")

NEEDS_REVIEW = "needs_review"
_TERMINAL = ("succeeded", "failed", NEEDS_REVIEW)


class TaskLockedError(RuntimeError):
    pass


class VideoResultValidationError(RuntimeError):
    """The provider returned a terminal result, but the persisted media is bad."""


class ModelSnapshotMismatchError(RuntimeError):
    """A task snapshot no longer matches the configured model gateway secret."""


def model_snapshot(model) -> dict:
    gateway_cfg = runtime_config_for_model(model, getattr(model, "use", None))
    return {
        "model_id": model.model_id,
        "cost_credits": int(model.cost_credits or 0),
        "unlock_cost": int(model.unlock_cost or 0),
        "extra": model.extra or {},
        "provider": gateway_cfg.provider,
        "base_url": gateway_cfg.base_url,
        "gateway_format": gateway_cfg.gateway_format,
        "gateway_source": gateway_cfg.source,
        "gateway_key_fingerprint": gateway_key_fingerprint(gateway_cfg),
    }


def _model_from_snapshot(task: GenTask, fallback_model):
    snapshot = ((task.params or {}).get("_model_snapshot") or {})
    if not snapshot:
        return fallback_model
    assert_model_snapshot_compatible(fallback_model, snapshot)
    return SimpleNamespace(
        model_id=snapshot.get("model_id") or fallback_model.model_id,
        cost_credits=int(snapshot.get("cost_credits") or 0),
        unlock_cost=int(snapshot.get("unlock_cost") or 0),
        extra=snapshot.get("extra") or {},
        provider=snapshot.get("provider"),
        base_url=snapshot.get("base_url"),
        api_key_encrypted=getattr(fallback_model, "api_key_encrypted", None),
        gateway_format=snapshot.get("gateway_format"),
        gateway_source=snapshot.get("gateway_source"),
        use=getattr(fallback_model, "use", None) or task.category,
        enabled=True,
    )


def assert_model_snapshot_compatible(model, snapshot: dict) -> None:
    expected = (snapshot or {}).get("gateway_key_fingerprint")
    if not expected:
        return
    current_cfg = runtime_config_for_model(model, getattr(model, "use", None))
    if gateway_key_fingerprint(current_cfg) != expected:
        raise ModelSnapshotMismatchError(
            "模型网关配置已变更,该任务快照不能继续使用。请重新生成预览后再试。"
        )


def _gateway_config_from_model(model, use: str) -> RuntimeGatewayConfig:
    cfg = runtime_config_for_model(model, use)
    expected = getattr(model, "gateway_key_fingerprint", None)
    if expected and expected != gateway_key_fingerprint(cfg):
        raise ModelSnapshotMismatchError(
            "模型网关配置已变更,该任务快照不能继续使用。请重新生成预览后再试。"
        )
    return cfg


def _accepts_gateway_config(fn) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    return "gateway_config" in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )


def _gen_image_with_model_config(model, prompt: str, *, n: int, size: str,
                                 reference_image_url: str | None,
                                 edit_path: str | None,
                                 extra_payload: dict | None) -> list[bytes]:
    kwargs = {
        "n": n,
        "size": size,
        "reference_image_url": reference_image_url,
        "edit_path": edit_path,
        "extra_payload": extra_payload,
    }
    if _accepts_gateway_config(gateway.gen_image):
        kwargs["gateway_config"] = _gateway_config_from_model(model, "image")
    return gateway.gen_image(prompt, model.model_id, **kwargs)


def _submit_video_with_model_config(model, prompt: str, params: dict) -> str:
    kwargs = {"extra": model.extra}
    if _accepts_gateway_config(gateway.submit_video):
        kwargs["gateway_config"] = _gateway_config_from_model(model, "video")
    return gateway.submit_video(prompt, model.model_id, params, **kwargs)


def _poll_video_with_model_config(model, external_task_id: str) -> dict:
    kwargs = {"extra": model.extra}
    if _accepts_gateway_config(gateway.poll_video):
        kwargs["gateway_config"] = _gateway_config_from_model(model, "video")
    return gateway.poll_video(external_task_id, model.model_id, **kwargs)


def claim_terminal(db, task_id: int, status: str, *, error: str | None = None,
                   cost_settled: int | None = None) -> bool:
    """Atomically move a task into a terminal state.

    Returns True iff THIS call performed the transition — so settle/refund is
    done exactly once even when the worker, the reaper and a duplicate delivery
    race (the Redis lock is only best-effort). The conditional UPDATE takes a
    row lock, so a concurrent finaliser sees the already-terminal status and
    gets rowcount 0."""
    values: dict = {"status": status, "finished_at": datetime.now(timezone.utc)}
    if error is not None:
        values["error"] = error[:1000]
    if cost_settled is not None:
        values["cost_settled"] = cost_settled
    res = db.execute(
        update(GenTask)
        .where(GenTask.id == task_id, GenTask.status.not_in(_TERMINAL))
        .values(**values)
    )
    return (res.rowcount or 0) == 1


def is_terminal_status(status: str | None) -> bool:
    return status in _TERMINAL

# Configurable so ops can tune render polling without code changes.
VIDEO_POLL_MAX_SECONDS = settings.video_poll_max_seconds
VIDEO_POLL_INTERVAL = settings.video_poll_interval_seconds
VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")

def _settlement_cost(task: GenTask, model, *, image_count: int | None = None) -> int:
    """Settled *credit* cost — must match what credits.settle can charge out of
    the frozen estimate. This is the internal credit price (image scales with n,
    video preview is cheap), NOT the provider's real $ cost; real per-call usage
    is logged separately in gateway_calls."""
    base = max(0, int(model.cost_credits or 0))
    if task.category == "video" and task.stage == "preview":
        try:
            base = int((model.extra or {}).get("preview_cost", max(1, base // 10)))
        except (TypeError, ValueError):
            base = max(1, base // 10)
    elif task.category == "image":
        n = int(image_count if image_count is not None else (task.params or {}).get("n") or 1)
        base = base * max(1, n)
    return max(0, min(base, int(task.cost_frozen or 0)))


def _final_prompt(task: GenTask) -> str:
    p = task.prompt or {}
    if p.get("final_text"):
        return p["final_text"]
    if p.get("instruction"):
        return p["instruction"]
    # reverse-off mode: instruction + reference hint
    return "generate a new image in the same style as the reference, high quality"


def _gateway_reference_image(db, task: GenTask, url: str | None) -> str | None:
    if not url:
        return None
    try:
        return asset_refs.gateway_ref_for_user_asset(db, task.user_id, url)
    except asset_refs.AssetRefError as e:
        raise RuntimeError(str(e)) from e


def _reference_dimensions(task: GenTask) -> tuple[int | None, int | None]:
    params = task.params or {}
    width = params.get("reference_width") or params.get("width")
    height = params.get("reference_height") or params.get("height")
    try:
        w = int(width)
        h = int(height)
    except (TypeError, ValueError):
        return None, None
    if w <= 0 or h <= 0:
        return None, None
    return w, h


def _closest_image_size(width: int | None, height: int | None,
                        fallback: str = "1024x1024") -> str:
    if not width or not height:
        return fallback
    ref_ratio = width / height
    try:
        fallback_w, fallback_h = (int(x) for x in str(fallback).split("x", 1))
        target_area = fallback_w * fallback_h
    except (TypeError, ValueError):
        target_area = 1024 * 1024

    def score(size: str) -> float:
        w, h = (int(x) for x in size.split("x", 1))
        ratio = w / h
        return abs(ratio - ref_ratio) + abs((w * h) - target_area) / 50_000_000

    return min(IMAGE_SIZES, key=score)


def _video_ratio(width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    ref = width / height
    candidates = {
        "1:1": 1.0,
        "3:4": 3 / 4,
        "4:3": 4 / 3,
        "9:16": 9 / 16,
        "16:9": 16 / 9,
    }
    return min(candidates, key=lambda k: abs(candidates[k] - ref))


def _video_target_resolution(params: dict, fallback: str = "720p") -> str:
    selected = params.get("target_resolution") or params.get("resolution") or fallback
    return selected if selected in VIDEO_RESOLUTIONS else fallback


def _video_target_duration(params: dict, fallback: int = 5) -> int:
    try:
        duration = int(params.get("target_duration") or params.get("duration") or fallback)
    except (TypeError, ValueError):
        duration = fallback
    return max(1, min(duration, settings.max_video_seconds))


def _video_preview_resolution(target_resolution: str) -> str:
    # Preview remains the cheap probe render; the selected quality is preserved
    # separately and used for the final render.
    return "480p" if target_resolution != "480p" else target_resolution


def _video_poster_url(task: GenTask, params: dict) -> str | None:
    return params.get("reference_image_url") or (
        task.source_asset_url if task.source_type == "image" else None
    )


def _localize_video_poster(url: str | None, written_keys: list[str]) -> str | None:
    if not url:
        return None
    try:
        raw = gateway.download_bytes_limited(
            url,
            max_bytes=int(settings.parse_localize_image_max_bytes),
            allowed_content_types=("image/",),
        )
        preview_png, _, _ = make_image_preview(
            raw,
            max_pixels=int(settings.parse_localize_image_max_pixels),
        )
        key = storage.save_bytes(preview_png, "preview", "png")
        written_keys.append(key)
        return storage.public_url(key)
    except Exception as e:  # noqa: BLE001
        log.warning("video poster localize failed: %s", e)
        return None


def _video_media_meta(key: str | None, fallback_duration: int | None) -> dict:
    if not key:
        return {"width": None, "height": None, "duration": fallback_duration}
    meta = video_frames.probe_media(str(storage.local_path(key)))
    return {
        "width": meta.get("width"),
        "height": meta.get("height"),
        "duration": int(round(meta["duration"])) if meta.get("duration") else fallback_duration,
    }


def run_image_task(task_id: int) -> None:
    lock_key = f"gen:lock:{task_id}"
    lock_token = locks.acquire(lock_key)
    if not lock_token:
        log.warning("image task %s locked by another worker, retry later", task_id)
        raise TaskLockedError(f"image task {task_id} locked")
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in ("succeeded", "failed"):
            return
        task.status = "running"
        db.commit()
        set_progress(task_id, 10, "running")

        model = get_model_config(db, "image")
        if not model or not model.enabled:
            raise RuntimeError("未配置可用的图像模型")
        model = _model_from_snapshot(task, model)

        n = int((task.params or {}).get("n") or get_setting(db, "image_n", 4))
        params = task.params or {}
        fallback_size = get_setting(db, "image_size", "1024x1024")
        ref_w, ref_h = _reference_dimensions(task)
        size = params.get("size") or _closest_image_size(ref_w, ref_h, fallback_size)
        prompt = _final_prompt(task)

        set_progress(task_id, 30, "running")
        # reverse-off (image+instruction -> image): pass the reference image to
        # the edit endpoint if one is configured on the image model.
        ref = None
        if (task.prompt or {}).get("instruction") and task.source_type == "image":
            ref = _gateway_reference_image(db, task, task.source_asset_url)
        # reverse-off (图+指令→图): default to the configured edit endpoint so the
        # reference image is actually used; admin can set extra.edit_path="" to off.
        edit_path = (model.extra or {}).get("edit_path", settings.image_edit_path) or None
        extra_payload = {
            "seed": params.get("seed"),
            "negative_prompt": params.get("negative_prompt") or params.get("negative"),
        }
        mode = "edit" if (ref and edit_path) else "txt2img"
        t0 = time.time()
        try:
            images = _gen_image_with_model_config(
                model,
                prompt,
                n=n,
                size=size,
                reference_image_url=ref,
                edit_path=edit_path,
                extra_payload=extra_payload,
            )
            if not images:
                raise RuntimeError("图片网关未返回任何结果")
        except Exception as e:  # noqa: BLE001
            usage.record_call(db, kind="image", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id, status="failed",
                              latency_ms=int((time.time() - t0) * 1000),
                              detail={"n": n, "size": size, "mode": mode,
                                      "error": str(e)[:300]})
            raise
        usage.record_call(db, kind="image", model_id=model.model_id,
                          user_id=task.user_id, task_id=task.id, status="ok",
                          latency_ms=int((time.time() - t0) * 1000),
                          detail={"n": n, "actual_n": len(images), "size": size, "mode": mode})

        set_progress(task_id, 70, "running")
        written_keys: list[str] = []
        saved_count = 0
        image_errors: list[str] = []
        for raw in images:
            try:
                preview_png, hd_w, hd_h = make_image_preview(
                    raw,
                    max_pixels=int(settings.generated_image_max_pixels),
                )
                model_ref_png, _, _ = make_model_reference(
                    raw,
                    max_pixels=int(settings.generated_image_max_pixels),
                )
                hd_key = storage.save_bytes(raw, "hd", image_ext(raw))
                pv_key = storage.save_bytes(preview_png, "preview", "png")
                model_ref_key = storage.save_bytes_named(
                    model_ref_png,
                    "model_ref",
                    pv_key.split("/", 1)[1],
                )
                written_keys += [hd_key, pv_key, model_ref_key]
                db.add(
                    GenAsset(
                        task_id=task.id,
                        user_id=task.user_id,
                        type="image",
                        preview_url=storage.public_url(pv_key),
                        hd_url=storage.public_url(hd_key),
                        watermarked=True,
                        unlocked=False,
                        width=hd_w,
                        height=hd_h,
                    )
                )
                saved_count += 1
            except Exception as e:  # noqa: BLE001
                image_errors.append(str(e)[:160])
                log.warning("image task %s skipped one invalid image: %s", task_id, e)
        if saved_count <= 0:
            raise RuntimeError("图片网关返回结果均无法解析")

        # finalize atomically: only the runner that claims the terminal status
        # settles, so a duplicate/raced run can't double-charge or double-credit.
        real_cost = _settlement_cost(task, model, image_count=saved_count)
        skipped_n = max(0, n - saved_count)
        partial_detail = (
            {
                "requested_n": n,
                "saved_n": saved_count,
                "skipped_n": skipped_n,
                "errors": image_errors[:5],
            }
            if saved_count < n or image_errors else None
        )
        if partial_detail:
            task.params = {
                **(task.params or {}),
                "_partial": True,
                "_requested_n": n,
                "_saved_n": saved_count,
                "_skipped_n": skipped_n,
                "_partial_errors": image_errors[:5],
            }
        if not claim_terminal(db, task_id, "succeeded", cost_settled=real_cost):
            db.rollback()  # another runner finalized -> discard our row + files
            _unlink_keys(written_keys)
            return
        try:
            credits.settle(db, task.user_id, reserved=task.cost_frozen,
                           real_cost=real_cost, biz_ref=task.id, commit=False)
            db.commit()
        except Exception:
            db.rollback()
            _unlink_keys(written_keys)
            raise
        if partial_detail:
            usage.record_call(db, kind="image", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id, status="ok",
                              detail=partial_detail)
        set_progress(task_id, 100, "succeeded")
    except Exception as e:  # noqa: BLE001
        log.exception("image task %s failed", task_id)
        _fail_and_refund(db, task_id, str(e), public_error="图片生成失败，已退回冻结积分，请稍后重试")
    finally:
        db.close()
        locks.release(lock_key, lock_token)


# Liveness TTL must exceed the worst-case single poll round-trip (a slow gateway
# can take gateway_timeout * (retries+1) + backoff), otherwise the key expires
# mid-tick and the resume beat spawns a duplicate poll chain.
_POLL_LIVENESS_TTL = max(180, VIDEO_POLL_INTERVAL * 3,
                         (settings.gateway_max_retries + 1) * 60)
_POLL_MAX_CONSEC_ERRORS = 3
_VIDEO_DOWNLOAD_MAX_ATTEMPTS = max(1, int(settings.video_download_max_attempts or 1))
_VIDEO_DOWNLOAD_LIVENESS_TTL = max(
    _POLL_LIVENESS_TTL,
    int(settings.video_download_timeout_seconds) * _VIDEO_DOWNLOAD_MAX_ATTEMPTS + 60,
)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _unlink_keys(keys) -> None:
    """Best-effort remove stored media files (used to clean up a finalizer that
    lost the terminal-claim race, so it doesn't orphan files on disk)."""
    for k in keys or []:
        try:
            storage.local_path(k).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _poll_alive_key(task_id: int) -> str:
    return f"video:poll:alive:{task_id}"


def _download_alive_key(task_id: int) -> str:
    return f"video:download:alive:{task_id}"


def _download_lock_key(task_id: int) -> str:
    return f"video:download:lock:{task_id}"


def _mark_poll_alive(task_id: int) -> None:
    try:
        redis_client.set(_poll_alive_key(task_id), "1", ex=_POLL_LIVENESS_TTL)
    except Exception:  # noqa: BLE001
        pass


def _mark_video_download_alive(task_id: int) -> None:
    try:
        redis_client.set(_download_alive_key(task_id), "1", ex=_VIDEO_DOWNLOAD_LIVENESS_TTL)
    except Exception:  # noqa: BLE001
        pass


def _bump_poll_errors(task_id: int) -> int:
    try:
        n = redis_client.incr(f"video:poll:err:{task_id}")
        redis_client.expire(f"video:poll:err:{task_id}", _POLL_LIVENESS_TTL)
        return int(n)
    except Exception:  # noqa: BLE001
        return 0


def _reset_poll_errors(task_id: int) -> None:
    try:
        redis_client.delete(f"video:poll:err:{task_id}")
    except Exception:  # noqa: BLE001
        pass


def _poll_chain_alive(task_id: int) -> bool:
    try:
        return bool(redis_client.get(_poll_alive_key(task_id)))
    except Exception:  # noqa: BLE001
        return False


def _video_download_alive(task_id: int) -> bool:
    try:
        return bool(redis_client.get(_download_alive_key(task_id)))
    except Exception:  # noqa: BLE001
        return False


def _enqueue_poll(task_id: int) -> None:
    """Schedule one poll tick. The poll re-enqueues itself until terminal, so a
    long render never holds a worker slot."""
    from ..tasks import poll_video_task
    try:
        poll_video_task.apply_async((task_id,), countdown=VIDEO_POLL_INTERVAL)
    except Exception:  # noqa: BLE001
        log.exception("failed to enqueue video poll for task %s", task_id)


def _finalize_or_retry_video_download(db, task: GenTask, model, result: dict) -> bool:
    """Return True when finalised; False when a recoverable download retry was queued."""
    lock_key = _download_lock_key(task.id)
    lock_token = locks.acquire(lock_key, ttl=_VIDEO_DOWNLOAD_LIVENESS_TTL)
    if not lock_token:
        _mark_video_download_alive(task.id)
        log.info("video task %s download/finalize already in progress", task.id)
        return False
    _mark_video_download_alive(task.id)
    try:
        _finalize_video_success(db, task, model, result)
        return True
    except VideoResultValidationError as e:
        usage.record_call(db, kind="video_download", model_id=model.model_id,
                          user_id=task.user_id, task_id=task.id,
                          status="failed",
                          detail={"stage": task.stage,
                                  "external_task_id": task.external_task_id,
                                  "permanent": True,
                                  "error": str(e)[:300]})
        _fail_and_refund(db, task.id, str(e), public_error="视频结果下载失败，已退回冻结积分，请稍后重试")
        return True
    except Exception as e:  # noqa: BLE001
        _mark_video_download_alive(task.id)
        params = dict(task.params or {})
        attempts = int(params.get("_video_download_attempts") or 0)
        if attempts < _VIDEO_DOWNLOAD_MAX_ATTEMPTS:
            log.warning(
                "video task %s download/finalize attempt %s/%s failed: %s",
                task.id,
                attempts,
                _VIDEO_DOWNLOAD_MAX_ATTEMPTS,
                e,
            )
            usage.record_call(db, kind="video_download", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id,
                              status="failed",
                              detail={"stage": task.stage,
                                      "external_task_id": task.external_task_id,
                                      "attempt": attempts,
                                      "error": str(e)[:300]})
            _enqueue_poll(task.id)
            return False
        raise
    finally:
        locks.release(lock_key, lock_token)


def _video_submit_params(db, task: GenTask) -> dict:
    """Stage-aware effective gateway params for a video render."""
    params = dict(task.params or {})
    target_resolution = _video_target_resolution(params)
    target_duration = _video_target_duration(params)
    # preview = cheap/short low-res render; final = selected quality
    if task.stage == "preview":
        params["target_resolution"] = target_resolution
        params["target_duration"] = target_duration
        params["resolution"] = _video_preview_resolution(target_resolution)
        params["duration"] = min(target_duration, 5)
    else:
        params["target_resolution"] = target_resolution
        params["target_duration"] = target_duration
        params["resolution"] = target_resolution
        params["duration"] = target_duration
    ref_w, ref_h = _reference_dimensions(task)
    if not params.get("ratio"):
        params["ratio"] = _video_ratio(ref_w, ref_h)
    # image-to-video: every client-supplied first-frame/reference URL must be
    # resolved by this backend before it reaches the model gateway. Do not forward
    # user URLs directly, even after SSRF validation, because the gateway would
    # fetch them outside our ownership/content checks.
    first_frame = params.get("first_frame_image") or params.get("reference_image_url")
    if task.source_type == "image":
        first_frame = first_frame or task.source_asset_url
    if first_frame:
        safe_ref = _gateway_reference_image(db, task, first_frame)
        params["first_frame_image"] = safe_ref
        if params.get("reference_image_url"):
            params["reference_image_url"] = safe_ref
    return params


def _video_persisted_params(task: GenTask, original: dict, submitted: dict) -> dict:
    """Persist user intent, not just the low-cost preview submit envelope.

    Preview submits deliberately lower duration/resolution. If we store that
    envelope as-is, the later final render inherits 5s/480p instead of the
    selected full-quality target.
    """
    persisted = dict(original)
    target_resolution = submitted.get("target_resolution")
    target_duration = submitted.get("target_duration")
    if target_resolution:
        persisted["target_resolution"] = target_resolution
    if target_duration:
        persisted["target_duration"] = target_duration
    if task.stage == "preview":
        if target_resolution:
            persisted["resolution"] = target_resolution
        if target_duration:
            persisted["duration"] = target_duration
    else:
        if submitted.get("resolution"):
            persisted["resolution"] = submitted["resolution"]
        if submitted.get("duration"):
            persisted["duration"] = submitted["duration"]
    for key in ("ratio",):
        if submitted.get(key):
            persisted[key] = submitted[key]
    if original.get("first_frame_image"):
        persisted["first_frame_image"] = original["first_frame_image"]
    elif task.source_type == "image" and task.source_asset_url:
        persisted["first_frame_image"] = task.source_asset_url
    if task.stage != "preview":
        return persisted
    persisted["preview_resolution"] = submitted.get("resolution")
    persisted["preview_duration"] = submitted.get("duration")
    return persisted


def start_video_task(task_id: int) -> None:
    """Submit the async video render, then hand off to the non-blocking poller.

    The worker returns as soon as the job is submitted; a self-re-enqueuing poll
    task drives it to completion, so a long render never holds a worker slot.
    State (external_task_id, submit time, effective params, phase) is persisted
    so the render can be resumed from the DB even if this worker dies."""
    lock_key = f"gen:lock:{task_id}"
    lock_token = locks.acquire(lock_key)
    if not lock_token:
        log.warning("video task %s locked by another worker, retry later", task_id)
        raise TaskLockedError(f"video task {task_id} locked")
    submitted = False
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in _TERMINAL:
            return
        # Idempotent under acks_late redelivery: if the external job was already
        # submitted, NEVER submit again (that would create a duplicate render and
        # overwrite the task id) — just (re)attach the poll chain and return.
        if task.external_task_id:
            if task.status != "running" or task.phase != "polling":
                task.status = "running"
                task.phase = "polling"
                db.commit()
            submitted = True
        else:
            # Atomic claim: only the runner that moves queued -> running submits,
            # so a redelivery can't double-submit and overwrite the external id.
            claimed = db.execute(
                update(GenTask)
                .where(GenTask.id == task_id, GenTask.status == "queued")
                .values(status="running", phase="submitting")
            ).rowcount
            if (claimed or 0) != 1:
                return  # not queued (already being handled / stale) -> don't submit
            db.commit()
            set_progress(task_id, 10, "running")

            model = get_model_config(db, "video")
            if not model or not model.enabled:
                raise RuntimeError("未配置可用的视频模型")
            model = _model_from_snapshot(task, model)

            prompt = _final_prompt(task)
            original_params = dict(task.params or {})
            if not original_params.get("_video_request_id"):
                original_params["_video_request_id"] = f"video-{task.id}-{uuid4().hex}"
                task.params = original_params
                db.commit()
            params = _video_submit_params(db, task)
            params.setdefault("request_id", original_params["_video_request_id"])

            submit_t0 = time.time()
            try:
                ext_id = _submit_video_with_model_config(model, prompt, params)
            except Exception as e:  # noqa: BLE001
                unknown = _submit_state_unknown(e)
                usage.record_call(db, kind="video_submit", model_id=model.model_id,
                                  user_id=task.user_id, task_id=task.id, status="failed",
                                  latency_ms=int((time.time() - submit_t0) * 1000),
                                  detail={"stage": task.stage, "resolution": params.get("resolution"),
                                          "target_resolution": params.get("target_resolution"),
                                          "duration": params.get("duration"),
                                          "target_duration": params.get("target_duration"),
                                          "ratio": params.get("ratio"),
                                          "request_id": params.get("request_id"),
                                          "submit_state_unknown": unknown,
                                          "error": str(e)[:300]})
                if unknown:
                    _hold_for_reconciliation(db, task_id, str(e))
                    return
                raise
            usage.record_call(db, kind="video_submit", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id, status="ok",
                              latency_ms=int((time.time() - submit_t0) * 1000),
                              detail={"stage": task.stage, "external_task_id": ext_id,
                                      "resolution": params.get("resolution"),
                                      "target_resolution": params.get("target_resolution"),
                                      "duration": params.get("duration"),
                                      "target_duration": params.get("target_duration"),
                                      "ratio": params.get("ratio")})
            # Persist the external job handle and recoverable target params. For
            # preview, keep the user's final-quality target instead of the cheap
            # preview submit envelope.
            task.params = _video_persisted_params(task, original_params, params)
            task.external_task_id = ext_id
            task.external_submitted_at = datetime.now(timezone.utc)
            task.phase = "polling"
            db.commit()
            set_progress(task_id, 30, "running")
            submitted = True
    except Exception as e:  # noqa: BLE001
        log.exception("video submit %s failed", task_id)
        _fail_and_refund(db, task_id, str(e), public_error="视频提交失败，已退回冻结积分，请稍后重试")
    finally:
        db.close()
        locks.release(lock_key, lock_token)

    if submitted:
        _mark_poll_alive(task_id)
        _enqueue_poll(task_id)


def poll_video_once(task_id: int) -> None:
    """One poll tick: re-enqueues itself until the render is terminal, finalises
    on success/failure, and refunds on a real (submit-time) render timeout."""
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in _TERMINAL:
            return
        if task.category != "video" or not task.external_task_id:
            return
        model = get_model_config(db, "video")
        if not model:
            _fail_and_refund(db, task_id, "视频模型配置缺失")
            return
        model = _model_from_snapshot(task, model)

        _mark_poll_alive(task_id)  # tell the recovery beat this chain is alive

        if task.phase == "downloading" and (task.params or {}).get("_video_result_url"):
            if _finalize_or_retry_video_download(
                db,
                task,
                model,
                {"status": "succeeded", "url": (task.params or {}).get("_video_result_url")},
            ):
                return
            return

        submitted_at = _aware(task.external_submitted_at) or _aware(task.created_at)
        elapsed = ((datetime.now(timezone.utc) - submitted_at).total_seconds()
                   if submitted_at else 0)
        if elapsed > VIDEO_POLL_MAX_SECONDS:
            usage.record_call(db, kind="video_poll", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id, status="failed",
                              detail={"stage": task.stage,
                                      "external_task_id": task.external_task_id,
                                      "error": "render timeout"})
            _fail_and_refund(db, task_id, "视频渲染超时")
            return

        try:
            res = _poll_video_with_model_config(model, task.external_task_id)
        except Exception as e:  # noqa: BLE001
            if isinstance(e, gateway.GatewayError) and getattr(e, "transient", False):
                usage.record_call(db, kind="video_poll", model_id=model.model_id,
                                  user_id=task.user_id, task_id=task.id,
                                  status="failed",
                                  detail={"stage": task.stage,
                                          "external_task_id": task.external_task_id,
                                          "transient": True,
                                          "error": str(e)[:300]})
                _enqueue_poll(task_id)
                return
            # a few consecutive failures (e.g. a terminal 4xx / unknown task id)
            # should fail fast, not re-poll for the whole 15-min budget.
            fails = _bump_poll_errors(task_id)
            log.warning("poll error %s/%s for task %s: %s",
                        fails, _POLL_MAX_CONSEC_ERRORS, task_id, e)
            if fails >= _POLL_MAX_CONSEC_ERRORS:
                _fail_and_refund(
                    db,
                    task_id,
                    f"视频轮询连续失败: {e}",
                    public_error="视频状态查询失败，已退回冻结积分，请稍后重试",
                )
            else:
                _enqueue_poll(task_id)
            return
        _reset_poll_errors(task_id)
        _mark_poll_alive(task_id)  # refresh after the (possibly slow) poll too

        status = res.get("status")
        if status == "failed":
            usage.record_call(db, kind="video_poll", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id, status="failed",
                              detail={"stage": task.stage,
                                      "external_task_id": task.external_task_id,
                                      "error": str(res.get("error"))[:300]})
            _fail_and_refund(
                db,
                task_id,
                res.get("error") or "视频网关返回失败",
                public_error="视频生成失败，已退回冻结积分，请稍后重试",
            )
            return
        if status == "succeeded":
            usage.record_call(db, kind="video_poll", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id, status="ok",
                              detail={"stage": task.stage,
                                      "external_task_id": task.external_task_id})
            _finalize_or_retry_video_download(db, task, model, res)
            return

        # still queued/running -> advance progress and poll again later
        pct = min(85, 30 + int(elapsed) * 55 // max(1, VIDEO_POLL_MAX_SECONDS))
        set_progress(task_id, pct, "running")
        _enqueue_poll(task_id)
    except Exception as e:  # noqa: BLE001
        log.exception("poll_video_once %s failed", task_id)
        _fail_and_refund(db, task_id, str(e), public_error="视频生成失败，已退回冻结积分，请稍后重试")
    finally:
        db.close()


def _finalize_video_success(db, task: GenTask, model, result: dict) -> None:
    params = dict(task.params or {})
    video_url = result.get("url") or params.get("_video_result_url")
    # Only mock mode may legitimately lack a URL; a real "succeeded" with no URL
    # is a failure (don't fabricate a placeholder and charge for it).
    if not result.get("mock") and not video_url:
        raise VideoResultValidationError("视频网关返回成功但未提供视频地址")
    if video_url:
        params["_video_result_url"] = video_url
    params["_video_download_attempts"] = int(params.get("_video_download_attempts") or 0) + 1
    task.params = params
    task.phase = "downloading"
    db.commit()
    _mark_video_download_alive(task.id)

    media_meta = {"width": None, "height": None,
                  "duration": int(params.get("duration", 0)) or None}
    written_keys: list[str] = []
    if result.get("mock") or not video_url:
        # mock / no direct url -> placeholder still so the flow is demonstrable
        still = gateway.mock_video_preview_image()
        pv_key = storage.save_bytes(still, "preview", "png")
        written_keys.append(pv_key)
        preview_url = storage.public_url(pv_key)
        hd_url = preview_url if task.stage == "final" else None
    else:
        # download + store the mp4 locally (Ark URLs expire within ~24h). If we
        # can't persist it, FAIL the task (-> refund) rather than charge for a
        # result behind an expiring URL that will soon 404.
        try:
            set_progress(task.id, 92, "running")
            _mark_video_download_alive(task.id)
            media_subdir = "video_hd" if task.stage == "final" else "video_preview"
            media_key = gateway.download_to_storage(
                video_url,
                media_subdir,
                "mp4",
                max_bytes=int(settings.video_download_max_bytes),
                timeout_seconds=int(settings.video_download_timeout_seconds),
                allowed_content_types=("video/", "application/octet-stream", "binary/octet-stream"),
            )
            _mark_video_download_alive(task.id)
            written_keys.append(media_key)
            local = storage.public_url(media_key)
            media_meta = _video_media_meta(media_key, int(params.get("duration", 0)) or None)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"视频结果下载落盘失败: {e}") from e
        # Reject non-video content: a gateway returning an HTML error page or a
        # corrupt file would otherwise be saved + charged as a "video". Real
        # video mode requires ffprobe so this check cannot silently disappear on
        # a misbuilt worker image.
        if not settings.effective_video_mock and not video_frames.FFPROBE:
            _unlink_keys(written_keys)
            raise VideoResultValidationError("服务器缺少 ffprobe,无法校验视频结果")
        if video_frames.FFPROBE and not media_meta.get("width"):
            _unlink_keys(written_keys)
            raise VideoResultValidationError("视频网关返回的不是有效视频(无视频流)")
        if task.stage == "final":
            # gate full video behind unlock; poster = the source first frame
            hd_url = local
            preview_url = _localize_video_poster(_video_poster_url(task, params), written_keys)
            if not preview_url:
                # pure text-to-video has no reference poster -> grab the rendered
                # clip's first frame so the locked asset isn't blank in the UI.
                poster = video_frames.extract_poster(str(storage.local_path(media_key)))
                if poster:
                    poster_key = storage.save_bytes(poster, "preview", "jpg")
                    written_keys.append(poster_key)
                    preview_url = storage.public_url(poster_key)
            if not preview_url:
                # Last-resort public poster. Never expose the locked final video
                # URL as preview_url; /media/video_hd is intentionally not mounted.
                still = gateway.mock_video_preview_image()
                poster_key = storage.save_bytes(still, "preview", "png")
                written_keys.append(poster_key)
                preview_url = storage.public_url(poster_key)
        else:
            # preview stage is watchable for free
            preview_url = local
            hd_url = None

    db.add(GenAsset(
        task_id=task.id, user_id=task.user_id, type="video",
        preview_url=preview_url, hd_url=hd_url, watermarked=True,
        unlocked=(task.stage != "final"),
        width=media_meta.get("width"), height=media_meta.get("height"),
        duration=media_meta.get("duration"),
    ))
    real_cost = _settlement_cost(task, model)
    if not claim_terminal(db, task.id, "succeeded", cost_settled=real_cost):
        db.rollback()  # another runner finalized first -> discard our row + files
        _unlink_keys(written_keys)
        return
    try:
        credits.settle(db, task.user_id, reserved=task.cost_frozen,
                       real_cost=real_cost, biz_ref=task.id, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        _unlink_keys(written_keys)
        raise
    set_progress(task.id, 100, "succeeded")


def admin_settle_needs_review_video(
    db,
    task: GenTask,
    *,
    result_url: str,
    external_task_id: str | None = None,
) -> None:
    claimed_for_reconciliation = task.status == "running" and task.phase == "reconciling"
    if task.status != NEEDS_REVIEW and not claimed_for_reconciliation:
        raise ValueError("仅待对账任务可执行成功结算")
    if task.category != "video":
        raise ValueError("仅视频任务支持补结果结算")
    model = get_model_config(db, "video")
    if not model:
        raise ValueError("未配置视频模型")
    model = _model_from_snapshot(task, model)
    params = dict(task.params or {})
    if external_task_id:
        task.external_task_id = external_task_id
    task.phase = "downloading"
    task.error = None
    task.finished_at = None
    task.params = params
    db.flush()
    task_id = task.id
    try:
        _finalize_video_success(db, task, model, {"status": "succeeded", "url": result_url})
    except Exception as e:
        db.rollback()
        task = db.get(GenTask, task_id)
        if task is not None and task.status not in _TERMINAL:
            task.status = NEEDS_REVIEW
            task.phase = "reconciling"
            task.error = f"补结果结算失败:{str(e)[:900]}"
            task.finished_at = None
            db.commit()
            set_progress(task.id, 100, NEEDS_REVIEW)
        raise


def resume_stuck_videos(db) -> int:
    """Recovery: re-attach a poll to any in-flight video whose poll chain looks
    dead (liveness key expired), so a worker crash never strands a submitted
    external job. Healthy chains (alive key present) are left untouched."""
    rows = list(db.execute(
        select(GenTask).where(
            GenTask.status == "running",
            GenTask.category == "video",
            GenTask.external_task_id.isnot(None),
        )
    ).scalars())
    resumed = 0
    for t in rows:
        if _poll_chain_alive(t.id) or _video_download_alive(t.id):
            continue
        _mark_poll_alive(t.id)
        _enqueue_poll(t.id)
        resumed += 1
    if resumed:
        log.info("resumed %s stuck video poll chain(s)", resumed)
    return resumed


def _hold_for_reconciliation(db, task_id: int, error: str) -> None:
    """Terminal hold for video submits where the provider may already be working.

    The user's reservation stays frozen until an admin/operator checks the
    provider side and either refunds or settles manually. This avoids turning
    client-side submit timeouts into free duplicate upstream renders.
    """
    try:
        db.rollback()
        task = db.get(GenTask, task_id)
        if not task:
            return
        params = dict(task.params or {})
        request_id = params.get("_video_request_id")
        message = (
            "视频提交状态未知,已冻结额度等待人工对账。"
            f"request_id={request_id or 'unknown'}; error={error[:500]}"
        )
        if not claim_terminal(db, task_id, NEEDS_REVIEW, error=message):
            db.rollback()
            return
        db.commit()
        set_progress(task_id, 100, NEEDS_REVIEW)
    except Exception:
        log.exception("reconciliation hold failed for task %s", task_id)
        db.rollback()


def _submit_state_unknown(exc: Exception) -> bool:
    """True when the upstream submit may have been accepted.

    Non-idempotent submit calls use retries=0, so network timeouts, 429 and 5xx
    leave us unsure whether the provider accepted the render. Terminal 4xx
    request/auth errors are treated as definite failures and refunded.
    """
    if isinstance(exc, gateway.GatewayError):
        explicit = getattr(exc, "submit_state_unknown", None)
        if explicit is not None:
            return bool(explicit)
        status_code = getattr(exc, "status_code", None)
        if status_code is not None and status_code < 500 and status_code != 429:
            return False
        return bool(getattr(exc, "transient", False) or status_code is None or status_code >= 500)
    return True


def _fail_and_refund(db, task_id: int, error: str, *, public_error: str | None = None) -> None:
    try:
        db.rollback()  # clear any partial work from the failed attempt
        task = db.get(GenTask, task_id)
        if not task:
            return
        # Idempotent: only the runner that claims the failed transition refunds.
        # If another runner (success path / reaper / duplicate) already finalized
        # this task, rowcount is 0 and we must NOT refund again.
        if not claim_terminal(db, task_id, "failed", error=public_error or error):
            db.rollback()
            return
        if task.cost_frozen and task.cost_settled == 0:
            credits.refund(db, task.user_id, task.cost_frozen,
                           biz_ref=task.id, commit=False)
        db.commit()
        set_progress(task_id, 100, "failed")
    except Exception:
        log.exception("fail handler errored for task %s", task_id)
        db.rollback()
