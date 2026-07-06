"""Pure video-gateway payload and response helpers."""
from __future__ import annotations

VIDEO_STATUS = {
    "succeeded": "succeeded", "success": "succeeded", "completed": "succeeded",
    "complete": "succeeded",
    "done": "succeeded",
    "failed": "failed", "error": "failed", "cancelled": "failed", "canceled": "failed",
    "expired": "failed",
    "running": "running", "processing": "running", "in_progress": "running",
    "generating": "running",
    "queued": "queued", "pending": "queued", "submitted": "queued",
}

GENERIC_VIDEO_ALLOWED_PARAMS = {
    "duration",
    "resolution",
    "ratio",
    "seed",
    "request_id",
    "negative_prompt",
    "prompt_extend",
    "last_frame_image",
}


def generic_video_payload_params(params: dict, extra: dict) -> dict:
    allowed = set(GENERIC_VIDEO_ALLOWED_PARAMS)
    allowed.update(str(k) for k in (extra.get("allowed_param_fields") or []))
    payload_params = {
        k: v
        for k, v in dict(params or {}).items()
        if k in allowed
        and k != "last_frame_image"
        and not str(k).startswith("_")
        and v not in (None, "")
    }
    first_frame = (params or {}).get("first_frame_image")
    first_frame_field = extra.get("first_frame_field", "first_frame_image")
    if first_frame and first_frame_field:
        payload_params[str(first_frame_field)] = first_frame
    last_frame = (params or {}).get("last_frame_image")
    last_frame_field = extra.get("last_frame_field", "last_frame_image")
    if last_frame and last_frame_field:
        payload_params[str(last_frame_field)] = last_frame
    character = (params or {}).get("character_reference_image")
    character_field = extra.get("character_image_field", "character_reference_image")
    if character and character_field:
        payload_params[str(character_field)] = character
    return payload_params


def nested_video_url(value) -> str | None:
    if isinstance(value, dict):
        direct = (
            value.get("url")
            or value.get("video_url")
            or value.get("download_url")
        )
        if direct:
            return direct
        for child in value.values():
            url = nested_video_url(child)
            if url:
                return url
    if isinstance(value, list) and value:
        for item in value:
            url = nested_video_url(item)
            if url:
                return url
    return None


def extract_by_path(data, path: str | None):
    if not path:
        return None
    current = data
    for part in str(path).split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        return None
    return current


def ark_text(prompt: str, params: dict) -> str:
    """Seedance takes generation params as --flags appended to the text prompt."""
    parts = [prompt.strip()]
    res = params.get("resolution")
    dur = params.get("duration")
    ratio = params.get("ratio")
    seed = params.get("seed")
    if res:
        parts.append(f"--resolution {res}")
    if dur:
        parts.append(f"--duration {int(dur)}")
    if ratio:
        parts.append(f"--ratio {ratio}")
    if seed not in (None, ""):
        parts.append(f"--seed {int(seed)}")
    parts.append("--watermark false")
    return "  ".join(parts)


def ark_content(prompt: str, params: dict) -> list:
    content = [{"type": "text", "text": ark_text(prompt, params)}]
    seen_images = set()
    img = params.get("first_frame_image")
    if img:
        seen_images.add(str(img))
        content.append({"type": "image_url", "image_url": {"url": img}})
    last = params.get("last_frame_image")
    if last and str(last) not in seen_images:
        seen_images.add(str(last))
        content.append({"type": "image_url", "image_url": {"url": last}})
    character = params.get("character_reference_image")
    if character and str(character) not in seen_images:
        content.append({"type": "image_url", "image_url": {"url": character}})
    return content


def ark_payload(model_id: str, prompt: str, params: dict) -> dict:
    """Build the Ark video task body with native generation fields.

    Keep text flags in ``content`` for older gateways, but also send the fields
    Ark exposes at the request-body layer. Without these native fields, some
    gateways treat ``--resolution 1080p`` as plain prompt text and fall back to
    their default 480p output.
    """
    params = params or {}
    payload = {"model": model_id, "content": ark_content(prompt, params)}
    if params.get("resolution"):
        payload["resolution"] = str(params["resolution"])
    if params.get("duration") not in (None, ""):
        try:
            payload["duration"] = int(params["duration"])
        except (TypeError, ValueError):
            pass
    if params.get("ratio"):
        payload["ratio"] = str(params["ratio"])
    if params.get("seed") not in (None, ""):
        try:
            payload["seed"] = int(params["seed"])
        except (TypeError, ValueError):
            pass
    payload["watermark"] = False
    return payload
