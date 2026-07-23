from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image

from .config import ProviderSettings

CONTRACT_VERSION = "region-analyzer.v1"
CAPABILITIES = ("detector", "segmenter")


class EvidenceEngine(Protocol):
    def warmup(self) -> None: ...

    def analyze(self, image: Image.Image, capability: str) -> list[dict[str, Any]]: ...

    def health(self, capability: str) -> dict[str, Any]: ...


@dataclass(slots=True)
class _ModelState:
    model: Any = None
    transform: Any = None
    categories: tuple[str, ...] = ()
    analyzer: str = ""
    analyzer_version: str = ""
    error: str | None = None


def _normalized_box(
    box: list[float], *, width: int, height: int
) -> dict[str, float] | None:
    if width <= 0 or height <= 0 or len(box) != 4:
        return None
    left, top, right, bottom = box
    left = min(max(float(left), 0.0), float(width))
    top = min(max(float(top), 0.0), float(height))
    right = min(max(float(right), left), float(width))
    bottom = min(max(float(bottom), top), float(height))
    if right - left < 1 or bottom - top < 1:
        return None
    return {
        "x": round(left / width, 6),
        "y": round(top / height, 6),
        "width": round((right - left) / width, 6),
        "height": round((bottom - top) / height, 6),
    }


def _cross(
    origin: tuple[int, int], left: tuple[int, int], right: tuple[int, int]
) -> int:
    return (left[0] - origin[0]) * (right[1] - origin[1]) - (left[1] - origin[1]) * (
        right[0] - origin[0]
    )


def _convex_hull(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted(set(points))
    if len(ordered) <= 1:
        return ordered
    lower: list[tuple[int, int]] = []
    for point in ordered:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[int, int]] = []
    for point in reversed(ordered):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _mask_polygon(mask: Any) -> list[dict[str, float]] | None:
    import numpy as np

    rows, columns = np.nonzero(mask)
    if len(rows) < 3:
        return None
    height, width = mask.shape
    if width < 2 or height < 2:
        return None
    step = max(1, len(rows) // 4096)
    points = list(zip(columns[::step].tolist(), rows[::step].tolist(), strict=True))
    hull = _convex_hull(points)
    if len(hull) < 3:
        return None
    return [
        {
            "x": round(min(1.0, max(0.0, x / (width - 1))), 6),
            "y": round(min(1.0, max(0.0, y / (height - 1))), 6),
        }
        for x, y in hull[:128]
    ]


class TorchvisionEvidenceEngine:
    """CPU-friendly COCO/VOC evidence models with lazy, bounded inference."""

    def __init__(self, settings: ProviderSettings):
        self.settings = settings
        self._states = {capability: _ModelState() for capability in CAPABILITIES}
        self._load_locks = {capability: threading.Lock() for capability in CAPABILITIES}
        self._inference_locks = {
            capability: threading.Lock() for capability in CAPABILITIES
        }

    def warmup(self) -> None:
        for capability in CAPABILITIES:
            self._ensure_loaded(capability)

    def _ensure_loaded(self, capability: str) -> _ModelState:
        if capability not in self._states:
            raise ValueError("capability must be detector or segmenter")
        state = self._states[capability]
        if state.model is not None or state.error:
            return state
        with self._load_locks[capability]:
            if state.model is not None or state.error:
                return state
            try:
                if capability == "detector":
                    self._load_detector(state)
                else:
                    self._load_segmenter(state)
            except Exception as exc:  # noqa: BLE001
                state.error = f"{type(exc).__name__}: model initialization failed"
        return state

    def _load_detector(self, state: _ModelState) -> None:
        import torch
        import torchvision
        from torchvision.models.detection import (
            SSDLite320_MobileNet_V3_Large_Weights,
            ssdlite320_mobilenet_v3_large,
        )

        weights = SSDLite320_MobileNet_V3_Large_Weights.DEFAULT
        model = ssdlite320_mobilenet_v3_large(weights=weights)
        model.eval().to(torch.device(self.settings.device))
        state.model = model
        state.transform = weights.transforms()
        state.categories = tuple(weights.meta["categories"])
        state.analyzer = "torchvision_ssdlite320_mobilenet_v3_large"
        state.analyzer_version = f"torchvision-{torchvision.__version__}"

    def _load_segmenter(self, state: _ModelState) -> None:
        import torch
        import torchvision
        from torchvision.models.segmentation import (
            LRASPP_MobileNet_V3_Large_Weights,
            lraspp_mobilenet_v3_large,
        )

        weights = LRASPP_MobileNet_V3_Large_Weights.DEFAULT
        model = lraspp_mobilenet_v3_large(weights=weights)
        model.eval().to(torch.device(self.settings.device))
        state.model = model
        state.transform = weights.transforms()
        state.categories = tuple(weights.meta["categories"])
        state.analyzer = "torchvision_lraspp_mobilenet_v3_large"
        state.analyzer_version = f"torchvision-{torchvision.__version__}"

    def health(self, capability: str) -> dict[str, Any]:
        state = (
            self._ensure_loaded(capability)
            if self.settings.eager_load
            else self._states[capability]
        )
        if state.model is not None:
            return {
                "status": "available",
                "analyzer": state.analyzer,
                "analyzer_version": state.analyzer_version,
                "degraded_reason": None,
            }
        return {
            "status": "degraded",
            "analyzer": state.analyzer or f"torchvision_{capability}",
            "analyzer_version": state.analyzer_version or "unavailable",
            "degraded_reason": state.error or "model has not been loaded",
        }

    def analyze(self, image: Image.Image, capability: str) -> list[dict[str, Any]]:
        state = self._ensure_loaded(capability)
        if state.model is None:
            raise RuntimeError(state.error or f"{capability} model unavailable")
        with self._inference_locks[capability]:
            if capability == "detector":
                return self._detect(image, state)
            return self._segment(image, state)

    def _detect(self, image: Image.Image, state: _ModelState) -> list[dict[str, Any]]:
        import torch

        tensor = state.transform(image).to(torch.device(self.settings.device))
        with torch.inference_mode():
            output = state.model([tensor])[0]
        rows: list[dict[str, Any]] = []
        boxes = output["boxes"].detach().cpu().tolist()
        scores = output["scores"].detach().cpu().tolist()
        labels = output["labels"].detach().cpu().tolist()
        for box, score, label_index in zip(boxes, scores, labels, strict=True):
            if float(score) < self.settings.detector_score_threshold:
                continue
            bbox = _normalized_box(box, width=image.width, height=image.height)
            if bbox is None:
                continue
            label = (
                state.categories[int(label_index)]
                if 0 <= int(label_index) < len(state.categories)
                else f"class_{int(label_index)}"
            )
            rows.append(
                {
                    "label": label,
                    "text": label,
                    "confidence": round(min(1.0, max(0.0, float(score))), 6),
                    "bbox": bbox,
                }
            )
            if len(rows) >= self.settings.max_evidence_rows:
                break
        return rows

    def _segment(self, image: Image.Image, state: _ModelState) -> list[dict[str, Any]]:
        import numpy as np
        import torch

        tensor = (
            state.transform(image).unsqueeze(0).to(torch.device(self.settings.device))
        )
        with torch.inference_mode():
            logits = state.model(tensor)["out"][0]
            probabilities = torch.softmax(logits, dim=0)
            labels = torch.argmax(probabilities, dim=0)
        label_map = labels.detach().cpu().numpy()
        rows: list[dict[str, Any]] = []
        for label_index in np.unique(label_map).tolist():
            label_index = int(label_index)
            if label_index == 0:
                continue
            mask = label_map == label_index
            if int(mask.sum()) < self.settings.segmenter_min_pixels:
                continue
            confidence = float(
                probabilities[label_index][labels == label_index]
                .mean()
                .detach()
                .cpu()
                .item()
            )
            if confidence < self.settings.segmenter_score_threshold:
                continue
            polygon = _mask_polygon(mask)
            if polygon is None:
                continue
            label = (
                state.categories[label_index]
                if 0 <= label_index < len(state.categories)
                else f"class_{label_index}"
            )
            rows.append(
                {
                    "label": label,
                    "text": label,
                    "confidence": round(min(1.0, max(0.0, confidence)), 6),
                    "polygon": polygon,
                }
            )
            if len(rows) >= self.settings.max_evidence_rows:
                break
        return rows
