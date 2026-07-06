"""Image review settlement helpers."""
from __future__ import annotations

from sqlalchemy import func, select

from ..config import settings
from ..models import GenAsset, GenTask
from . import credits, gateway, storage
from .config_store import get_model_config
from .generation_common import publish_task_update, settlement_cost
from .generation_model_runtime import model_from_snapshot
from .generation_state import NEEDS_REVIEW, claim_terminal
from .generation_state import TERMINAL_STATUSES as TERMINAL
from .media_sidecars import unlink_keys
from .progress import set_progress
from .watermark import image_ext, make_image_preview


def admin_settle_needs_review_image(
    db,
    task: GenTask,
    *,
    result_url: str | None = None,
    get_model_config_fn=get_model_config,
) -> None:
    claimed_for_reconciliation = task.status == "running" and task.phase == "reconciling"
    if task.status != NEEDS_REVIEW and not claimed_for_reconciliation:
        raise ValueError("仅待对账任务可执行成功结算")
    if task.category != "image":
        raise ValueError("仅图片任务支持图片补结果结算")
    model = get_model_config_fn(db, "image")
    if not model:
        raise ValueError("未配置图片模型")
    model = model_from_snapshot(task, model)
    params = dict(task.params or {})
    keys = [str(key) for key in (params.get("_image_result_keys") or []) if key]
    saved_n = max(0, int(params.get("_saved_n") or 0))
    task.phase = "reconciling"
    task.error = None
    task.finished_at = None
    db.flush()
    task_id = task.id
    written_keys: list[str] = []
    try:
        existing_asset_count = int(
            db.execute(
                select(func.count(GenAsset.id)).where(GenAsset.task_id == task.id)
            ).scalar() or 0
        )
        image_count = existing_asset_count or max(1, saved_n)
        if existing_asset_count <= 0:
            if result_url:
                raw = gateway.download_bytes_limited(
                    result_url,
                    max_bytes=int(settings.generated_image_max_bytes),
                    allowed_content_types=("image/",),
                    timeout_seconds=int(settings.image_download_timeout_seconds),
                )
                preview_png, hd_w, hd_h = make_image_preview(
                    raw,
                    max_pixels=int(settings.generated_image_max_pixels),
                )
                hd_key = storage.save_bytes(raw, "hd", image_ext(raw))
                pv_key = storage.save_bytes(preview_png, "preview", "png")
                written_keys += [hd_key, pv_key]
                keys += [hd_key, pv_key]
                db.add(
                    GenAsset(
                        task_id=task.id,
                        user_id=task.user_id,
                        type="image",
                        preview_url=storage.public_url(pv_key),
                        hd_url=storage.public_url(hd_key),
                        watermarked=False,
                        unlocked=True,
                        width=hd_w,
                        height=hd_h,
                    )
                )
                image_count = 1
            else:
                hd_keys = [key for key in keys if key.startswith("hd/")]
                preview_keys = [key for key in keys if key.startswith("preview/")]
                if not hd_keys or not preview_keys:
                    raise ValueError("图片待对账任务缺少本地结果,请填写结果 URL")
                for idx, hd_key in enumerate(hd_keys):
                    preview_key = preview_keys[min(idx, len(preview_keys) - 1)]
                    hd_path = storage.local_path(hd_key)
                    preview_path = storage.local_path(preview_key)
                    if not hd_path.exists() or not preview_path.exists():
                        raise ValueError("图片待对账任务本地结果文件缺失,请填写结果 URL")
                    db.add(
                        GenAsset(
                            task_id=task.id,
                            user_id=task.user_id,
                            type="image",
                            preview_url=storage.public_url(preview_key),
                            hd_url=storage.public_url(hd_key),
                            watermarked=False,
                            unlocked=True,
                        )
                    )
                image_count = len(hd_keys)
        real_cost = settlement_cost(task, model, image_count=image_count)
        if not claim_terminal(db, task.id, "succeeded", cost_settled=real_cost):
            db.rollback()
            unlink_keys(written_keys)
            return
        credits.settle(
            db,
            task.user_id,
            reserved=task.cost_frozen,
            real_cost=real_cost,
            biz_ref=task.id,
            commit=False,
        )
        db.commit()
        set_progress(task.id, 100, "succeeded")
        publish_task_update(task, "succeeded")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        unlink_keys(written_keys)
        task = db.get(GenTask, task_id)
        if task is not None and task.status not in TERMINAL:
            task.status = NEEDS_REVIEW
            task.phase = "reconciling"
            task.error = f"图片补结果结算失败:{str(e)[:900]}"
            task.finished_at = None
            db.commit()
            set_progress(task.id, 100, NEEDS_REVIEW)
            publish_task_update(task, NEEDS_REVIEW)
        raise
