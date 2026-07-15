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


def generic_video_payload_params(params: dict, extra: dict, model_id: str = "") -> dict:
    allowed = set(GENERIC_VIDEO_ALLOWED_PARAMS)
    allowed.update(str(k) for k in (extra.get("allowed_param_fields") or []))
    payload_params = {
        k: v
        for k, v in dict(params or {}).items()
        if k in allowed
        and k
        not in {
            "first_frame_image",
            "last_frame_image",
            "product_reference_image",
            "character_reference_image",
            "style_reference_image",
        }
        and not str(k).startswith("_")
        and v not in (None, "")
    }
    first_frame = (params or {}).get("first_frame_image")
    is_grok_video = "grok" in str(model_id or "").strip().lower()
    first_frame_field = (
        extra.get("first_frame_field")
        if "first_frame_field" in extra
        else "image_url" if is_grok_video else "first_frame_image"
    )
    if first_frame and first_frame_field:
        payload_params[str(first_frame_field)] = first_frame
    last_frame = (params or {}).get("last_frame_image")
    last_frame_field = (
        extra.get("last_frame_field")
        if "last_frame_field" in extra
        else None if is_grok_video else "last_frame_image"
    )
    if last_frame and last_frame_field:
        payload_params[str(last_frame_field)] = last_frame
    product = (params or {}).get("product_reference_image")
    product_field = (
        extra.get("product_image_field")
        if "product_image_field" in extra
        else "image_url" if is_grok_video else "product_reference_image"
    )
    if (
        product
        and first_frame
        and product_field
        and first_frame_field
        and str(product_field) == str(first_frame_field)
        and str(product) != str(first_frame)
    ):
        raise ValueError("产品身份参考与首帧不能映射到同一供应商字段")
    if product and product_field:
        payload_params[str(product_field)] = product
    character = (params or {}).get("character_reference_image")
    character_field = extra.get("character_image_field", "character_reference_image")
    if character and character_field:
        payload_params[str(character_field)] = character
    style = (params or {}).get("style_reference_image")
    style_field = extra.get("style_image_field", "style_reference_image")
    if style and style_field:
        payload_params[str(style_field)] = style
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
    negative = str((params or {}).get("negative_prompt") or "").strip()
    if negative:
        parts.append(f"负向约束：{negative}")
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


def _bind_ark_product_reference(text: str, image_number: int) -> str:
    """Bind generic upload wording to Ark's required image-number syntax."""
    product_ref = f"图片{image_number}中的产品"
    for marker in (
        "上传的产品图片",
        "上传的产品图",
        "上传产品图片",
        "上传产品图",
        "上传产品",
    ):
        text = text.replace(marker, product_ref)
    return text


def ark_content(prompt: str, params: dict) -> list:
    content = [{"type": "text", "text": ark_text(prompt, params)}]
    seen_images: set[tuple[str, str]] = set()
    role_notes: list[str] = []

    def append_image(value, *, role: str) -> int | None:
        if not value:
            return None
        key = (str(value), role)
        if key in seen_images:
            return None
        seen_images.add(key)
        image_number = 1 + sum(item.get("type") == "image_url" for item in content)
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": value},
                "role": role,
            }
        )
        return image_number

    img = params.get("first_frame_image")
    first_number = append_image(img, role="first_frame")
    if first_number:
        role_notes.append(f"图片{first_number}为首帧")
    last = params.get("last_frame_image")
    last_number = append_image(last, role="last_frame")
    if last_number:
        role_notes.append(f"图片{last_number}为尾帧")
    product = params.get("product_reference_image")
    product_number = append_image(product, role="reference_image")
    if product_number:
        content[0]["text"] = _bind_ark_product_reference(
            content[0]["text"],
            product_number,
        )
        role_notes.append(
            f"图片{product_number}中的产品是唯一商品主体；图片{product_number}为产品身份参考，"
            "仅锁定同一SKU的包装、Logo、文字和材质纹理，不要求作为首帧"
        )
    style = params.get("style_reference_image")
    style_number = append_image(style, role="reference_image")
    if style_number:
        role_notes.append(
            f"图片{style_number}为风格参考，仅迁移色调、光线、材质和商业质感"
        )
    character = params.get("character_reference_image")
    character_number = append_image(character, role="reference_image")
    if character_number:
        role_notes.append(f"图片{character_number}为人物身份参考，仅锁定同一人物身份")
    if role_notes:
        content[0]["text"] = f"{content[0]['text']}  图片角色：{'；'.join(role_notes)}。"
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
    if params.get("negative_prompt"):
        payload["negative_prompt"] = str(params["negative_prompt"])
    if params.get("seed") not in (None, ""):
        try:
            payload["seed"] = int(params["seed"])
        except (TypeError, ValueError):
            pass
    payload["watermark"] = False
    return payload
