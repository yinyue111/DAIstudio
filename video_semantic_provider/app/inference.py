from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import ProviderSettings

CONTRACT_VERSION = "video-semantic-evidence.v1"
CAPABILITIES = ("subject_tracking", "pose", "action", "transition")
ANALYZER = "opencv_motion_subject_tracker"


class SemanticEngine(Protocol):
    def health(self) -> dict[str, Any]: ...

    def analyze(self, frames: list[dict[str, Any]]) -> dict[str, Any]: ...


@dataclass(slots=True)
class _Track:
    track_id: str
    segment: int
    observations: list[dict[str, Any]] = field(default_factory=list)


def _iou(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    x1, y1 = max(lx, rx), max(ly, ry)
    x2, y2 = min(lx + lw, rx + rw), min(ly + lh, ry + rh)
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    union = lw * lh + rw * rh - intersection
    return intersection / union if union else 0.0


def _bbox(
    box: tuple[int, int, int, int], *, width: int, height: int
) -> dict[str, float]:
    x, y, box_width, box_height = box
    return {
        "x": round(max(0, x) / width, 6),
        "y": round(max(0, y) / height, 6),
        "width": round(min(box_width, width - max(0, x)) / width, 6),
        "height": round(min(box_height, height - max(0, y)) / height, 6),
    }


class OpenCvSemanticEngine:
    """Tracks only sustained moving image regions; it never assigns object classes."""

    def __init__(self, settings: ProviderSettings):
        self.settings = settings

    def health(self) -> dict[str, Any]:
        try:
            import cv2
            import numpy
        except ImportError:
            return {
                "status": "degraded",
                "analyzer": ANALYZER,
                "analyzer_version": "unavailable",
                "degraded_reason": "OpenCV or NumPy is not installed",
                "capability_statuses": {
                    "subject_tracking": "degraded",
                    "pose": "unsupported",
                    "action": "unsupported",
                    "transition": "degraded",
                },
            }
        return {
            "status": "available",
            "analyzer": ANALYZER,
            "analyzer_version": f"opencv-{cv2.__version__};numpy-{numpy.__version__}",
            "degraded_reason": None,
            "capability_statuses": {
                "subject_tracking": "available",
                "pose": "unsupported",
                "action": "unsupported",
                "transition": "available",
            },
        }

    def analyze(self, frames: list[dict[str, Any]]) -> dict[str, Any]:
        health = self.health()
        if health["status"] != "available":
            return _failure(str(health["degraded_reason"]))
        tracks = self._tracks(frames)
        transitions = self._transitions(frames)
        return {
            "contract_version": CONTRACT_VERSION,
            "analyzer": health["analyzer"],
            "analyzer_version": health["analyzer_version"],
            "capabilities": {
                "subject_tracking": _result(
                    "analyzed" if tracks else "partial",
                    tracks,
                    None
                    if tracks
                    else "No sustained moving regions were detected in sampled frames",
                ),
                "pose": _result(
                    "unsupported",
                    [],
                    "This provider does not include a pose estimation model",
                ),
                "action": _result(
                    "unsupported",
                    [],
                    "This provider does not include an action recognition model",
                ),
                "transition": _result(
                    "analyzed" if transitions else "partial",
                    transitions,
                    None
                    if transitions
                    else "No abrupt visual discontinuity was detected in sampled frames",
                ),
            },
        }

    def _tracks(self, frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import cv2

        tracks: list[_Track] = []
        next_track = 1
        by_segment: dict[int, list[dict[str, Any]]] = {}
        for frame in frames:
            by_segment.setdefault(int(frame["source_segment_index"]), []).append(frame)
        for segment, rows in by_segment.items():
            rows.sort(
                key=lambda item: (
                    float(item["absolute_timestamp_seconds"]),
                    int(item["frame_index"]),
                )
            )
            prior_gray = None
            active: list[_Track] = []
            for row in rows:
                image = row["image"]
                gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
                gray = cv2.GaussianBlur(gray, (5, 5), 0)
                if prior_gray is None:
                    prior_gray = gray
                    continue
                difference = cv2.absdiff(prior_gray, gray)
                _, foreground = cv2.threshold(difference, 24, 255, cv2.THRESH_BINARY)
                foreground = cv2.morphologyEx(
                    foreground,
                    cv2.MORPH_OPEN,
                    cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
                )
                foreground = cv2.dilate(foreground, None, iterations=2)
                contours, _ = cv2.findContours(
                    foreground, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )
                detections: list[tuple[tuple[int, int, int, int], float]] = []
                image_area = image.shape[0] * image.shape[1]
                for contour in contours:
                    x, y, width, height = cv2.boundingRect(contour)
                    ratio = (width * height) / image_area
                    if (
                        not self.settings.min_motion_area_ratio
                        <= ratio
                        <= self.settings.max_motion_area_ratio
                    ):
                        continue
                    box = (x, y, width, height)
                    motion_strength = (
                        float(difference[y : y + height, x : x + width].mean()) / 255.0
                    )
                    confidence = round(min(0.95, max(0.25, motion_strength)), 6)
                    detections.append((box, confidence))
                used_tracks: set[str] = set()
                for box, confidence in detections:
                    candidates = [
                        track
                        for track in active
                        if track.track_id not in used_tracks and track.observations
                    ]
                    track = max(
                        candidates,
                        key=lambda candidate: _iou(
                            tuple(candidate.observations[-1]["pixel_bbox"]), box
                        ),
                        default=None,
                    )
                    if (
                        track is None
                        or _iou(tuple(track.observations[-1]["pixel_bbox"]), box)
                        < self.settings.track_iou_threshold
                    ):
                        track = _Track(
                            track_id=f"motion-subject-{segment}-{next_track}",
                            segment=segment,
                        )
                        next_track += 1
                        active.append(track)
                    used_tracks.add(track.track_id)
                    track.observations.append(
                        {
                            "frame_index": int(row["frame_index"]),
                            "absolute_timestamp_seconds": round(
                                float(row["absolute_timestamp_seconds"]), 3
                            ),
                            "bbox": _bbox(
                                box, width=image.shape[1], height=image.shape[0]
                            ),
                            "confidence": confidence,
                            "pixel_bbox": box,
                        }
                    )
                prior_gray = gray
            tracks.extend(track for track in active if len(track.observations) >= 2)

        result = []
        for track in tracks:
            observations = track.observations
            confidence = round(
                sum(item["confidence"] for item in observations) / len(observations), 6
            )
            result.append(
                {
                    "evidence_id": track.track_id,
                    "label": "moving_subject",
                    "source_segment_index": track.segment,
                    "start_seconds": observations[0]["absolute_timestamp_seconds"],
                    "end_seconds": observations[-1]["absolute_timestamp_seconds"],
                    "confidence": confidence,
                    "observations": [
                        {
                            key: value
                            for key, value in row.items()
                            if key != "pixel_bbox"
                        }
                        for row in observations
                    ],
                }
            )
        return result

    def _transitions(self, frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import cv2

        result: list[dict[str, Any]] = []
        by_segment: dict[int, list[dict[str, Any]]] = {}
        for frame in frames:
            by_segment.setdefault(int(frame["source_segment_index"]), []).append(frame)
        for segment, rows in by_segment.items():
            rows.sort(
                key=lambda item: (
                    float(item["absolute_timestamp_seconds"]),
                    int(item["frame_index"]),
                )
            )
            for before, after in zip(rows, rows[1:], strict=False):
                before_hist = cv2.calcHist(
                    [before["image"]], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3
                )
                after_hist = cv2.calcHist(
                    [after["image"]], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3
                )
                cv2.normalize(before_hist, before_hist)
                cv2.normalize(after_hist, after_hist)
                distance = float(
                    cv2.compareHist(before_hist, after_hist, cv2.HISTCMP_BHATTACHARYYA)
                )
                if distance < self.settings.transition_threshold:
                    continue
                before_time = float(before["absolute_timestamp_seconds"])
                after_time = float(after["absolute_timestamp_seconds"])
                result.append(
                    {
                        "evidence_id": f"transition-{segment}-{before['frame_index']}-{after['frame_index']}",
                        "label": "visual_discontinuity",
                        "source_segment_index": segment,
                        "timestamp_seconds": round((before_time + after_time) / 2, 3),
                        "before_frame_index": int(before["frame_index"]),
                        "after_frame_index": int(after["frame_index"]),
                        "confidence": round(min(0.99, max(0.0, distance)), 6),
                    }
                )
        return result


def _result(
    status: str, evidence: list[dict[str, Any]], reason: str | None
) -> dict[str, Any]:
    return {"status": status, "evidence": evidence, "degraded_reason": reason}


def _failure(reason: str) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "analyzer": ANALYZER,
        "analyzer_version": "unavailable",
        "capabilities": {
            "subject_tracking": _result("degraded", [], reason),
            "pose": _result(
                "unsupported",
                [],
                "This provider does not include a pose estimation model",
            ),
            "action": _result(
                "unsupported",
                [],
                "This provider does not include an action recognition model",
            ),
            "transition": _result("degraded", [], reason),
        },
    }
