"""OpenCV camera-motion evidence extraction."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from PIL import Image

# Closed labels used to reconcile optical flow with VLM camera prose.
CAMERA_MOTION_LABELS = ("pan", "tilt", "zoom", "static")
CAMERA_MOTION_DIRECTIONS = {
    "pan": ("left", "right"),
    "tilt": ("up", "down"),
    "zoom": ("in", "out"),
    "static": (),
}


def _stable_id(prefix: str, payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + hashlib.sha256(raw.encode()).hexdigest()[:20]


def cv2_motion_analysis(
    images: list[Image.Image],
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        import cv2  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except ImportError:
        return {
            "status": "unsupported", "analyzer": "opencv_lk_homography",
            "analyzer_version": "unavailable",
            "camera_labels": list(CAMERA_MOTION_LABELS), "samples": [],
            "excluded_cross_cut_pairs": 0,
            "degraded_reason": "服务器未安装 OpenCV/NumPy，无法形成光流运镜证据",
        }
    if len(images) < 2:
        return {
            "status": "degraded", "analyzer": "opencv_lk_homography",
            "analyzer_version": str(cv2.__version__),
            "camera_labels": list(CAMERA_MOTION_LABELS), "samples": [],
            "excluded_cross_cut_pairs": 0,
            "degraded_reason": "至少需要两帧才能分析运动",
        }
    samples: list[dict[str, Any]] = []
    excluded_cross_cut_pairs = 0
    for index in range(1, len(images)):
        previous_meta, current_meta = frames[index - 1], frames[index]
        if previous_meta["source_segment_index"] != current_meta["source_segment_index"]:
            continue
        previous_shot = previous_meta.get("detected_shot_index")
        current_shot = current_meta.get("detected_shot_index")
        if (
            isinstance(previous_shot, int)
            and isinstance(current_shot, int)
            and previous_shot != current_shot
        ):
            # Cross-cut frame pairs describe an edit jump, not camera movement.
            excluded_cross_cut_pairs += 1
            continue
        previous = cv2.cvtColor(np.array(images[index - 1]), cv2.COLOR_RGB2GRAY)
        current = cv2.cvtColor(np.array(images[index]), cv2.COLOR_RGB2GRAY)
        points = cv2.goodFeaturesToTrack(
            previous, maxCorners=200, qualityLevel=0.01, minDistance=7
        )
        if points is None or len(points) < 4:
            continue
        moved, status, _error = cv2.calcOpticalFlowPyrLK(previous, current, points, None)
        if moved is None or status is None:
            continue
        source_points = points[status.reshape(-1) == 1]
        target_points = moved[status.reshape(-1) == 1]
        if len(source_points) < 4:
            continue
        matrix, inliers = cv2.estimateAffinePartial2D(
            source_points, target_points, method=cv2.RANSAC, ransacReprojThreshold=3.0
        )
        if matrix is None:
            continue
        width, height = previous.shape[1], previous.shape[0]
        center_x, center_y = width / 2, height / 2
        moved_center_x = (
            float(matrix[0, 0]) * center_x
            + float(matrix[0, 1]) * center_y
            + float(matrix[0, 2])
        )
        moved_center_y = (
            float(matrix[1, 0]) * center_x
            + float(matrix[1, 1]) * center_y
            + float(matrix[1, 2])
        )
        dx = (moved_center_x - center_x) / width
        dy = (moved_center_y - center_y) / height
        scale = (float(matrix[0, 0]) ** 2 + float(matrix[0, 1]) ** 2) ** 0.5
        pan = min(1.0, abs(dx) * 20)
        tilt = min(1.0, abs(dy) * 20)
        zoom = min(1.0, abs(scale - 1) * 20)
        static = max(0.0, 1.0 - max(pan, tilt, zoom))
        labels = {"pan": pan, "tilt": tilt, "zoom": zoom, "static": static}
        camera = max(labels, key=labels.get)
        if camera == "pan":
            camera_direction = "left" if dx > 0 else "right"
        elif camera == "tilt":
            camera_direction = "up" if dy > 0 else "down"
        elif camera == "zoom":
            camera_direction = "in" if scale >= 1 else "out"
        else:
            camera_direction = None
        inlier_ratio = float(inliers.mean()) if inliers is not None else 0.0
        sample = {
            "start_seconds": previous_meta["timestamp_seconds"],
            "end_seconds": current_meta["timestamp_seconds"],
            "source_segment_index": current_meta["source_segment_index"],
            "frame_indices": [previous_meta["frame_index"], current_meta["frame_index"]],
            "camera": camera,
            "camera_direction": camera_direction,
            "camera_confidence": round(labels[camera], 6),
            "camera_scores": {key: round(value, 6) for key, value in labels.items()},
            "background_motion_confidence": round(inlier_ratio, 6),
            "subject_motion_confidence": round(max(0.0, 1.0 - inlier_ratio), 6),
        }
        sample["evidence_id"] = _stable_id("motion-", sample)
        samples.append(sample)
    if samples:
        degraded_reason = None
    elif excluded_cross_cut_pairs:
        degraded_reason = "抽样帧对全部跨越镜头切点，无法形成同镜头光流证据"
    else:
        degraded_reason = "抽样帧缺少足够可跟踪特征"
    return {
        "status": "analyzed" if samples else "degraded",
        "analyzer": "opencv_lk_homography",
        "analyzer_version": str(cv2.__version__),
        "camera_labels": list(CAMERA_MOTION_LABELS),
        "samples": samples,
        "excluded_cross_cut_pairs": excluded_cross_cut_pairs,
        "degraded_reason": degraded_reason,
    }
