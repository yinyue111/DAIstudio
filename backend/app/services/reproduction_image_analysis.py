"""Image-side reproduction assessment analysis engine.

Split out of :mod:`app.services.reproduction_assessment`; the function is a
verbatim move.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from . import image_evidence_analysis
from .reproduction_comparators import (
    _SCORE_THRESHOLD,
    _analyzer_record,
    _difference_regions,
    _heatmap,
    _image_pair,
    _image_subject_semantics,
    _ocr_dimension,
    _ocr_record,
    _open_image,
    _severity,
)


def _image_analysis(source_path: Path, generated_path: Path) -> dict[str, Any]:
    source_image = _open_image(source_path)
    generated_image = _open_image(generated_path)
    pair = _image_pair(source_image, generated_image)
    analyzers = {
        "pillow": _analyzer_record(status="analyzed", version=Image.__version__),
        "numpy": _analyzer_record(status="analyzed", version=np.__version__),
        "opencv": _analyzer_record(status="analyzed", version=cv2.__version__),
    }
    dimensions: dict[str, dict[str, Any]] = {
        "structure_layout": {
            "status": "analyzed",
            "score": pair["structure_layout"],
            "components": pair["components"],
        },
        "color_light": {"status": "analyzed", "score": pair["color_light"]},
        "detail_material": {"status": "analyzed", "score": pair["detail_material"]},
    }
    findings: list[dict[str, Any]] = []
    regions = _difference_regions(pair["diff"])
    structure_score = float(pair["structure_layout"])
    if structure_score < _SCORE_THRESHOLD:
        for index, bbox in enumerate(regions, start=1):
            findings.append(
                {
                    "finding_key": f"image-structure-{index}",
                    "dimension": "structure_layout",
                    "kind": "spatial_difference",
                    "severity": _severity(structure_score),
                    "confidence": round(1.0 - structure_score, 6),
                    "message": "构图或局部结构与源素材存在明显差异",
                    "bbox": bbox,
                    "time_range": None,
                    "shot_id": None,
                    "evidence": {"heatmap_region": bbox},
                    "metrics": {"score": round(structure_score, 6)},
                }
            )
    for dimension, message in (
        ("color_light", "色彩或光线与源素材不一致"),
        ("detail_material", "细节与材质表现与源素材不一致"),
    ):
        score = float(pair[dimension])
        if score < _SCORE_THRESHOLD:
            findings.append(
                {
                    "finding_key": f"image-{dimension}",
                    "dimension": dimension,
                    "kind": "global_difference",
                    "severity": _severity(score),
                    "confidence": round(1.0 - score, 6),
                    "message": message,
                    "bbox": regions[0],
                    "time_range": None,
                    "shot_id": None,
                    "evidence": {},
                    "metrics": {"score": round(score, 6)},
                }
            )

    source_ocr = image_evidence_analysis.tesseract_ocr(source_image)
    generated_ocr = image_evidence_analysis.tesseract_ocr(generated_image)
    analyzers["ocr_source"] = _ocr_record(source_ocr)
    analyzers["ocr_generated"] = _ocr_record(generated_ocr)
    ocr_dimension, ocr_finding = _ocr_dimension(source_ocr, generated_ocr)
    dimensions["ocr_text"] = ocr_dimension
    if ocr_finding:
        findings.append(ocr_finding)
    subject_dimension, subject_analyzer, subject_finding = _image_subject_semantics(
        source_image, generated_image
    )
    dimensions["subject_semantics"] = subject_dimension
    analyzers["subject_semantics"] = subject_analyzer
    if subject_finding:
        findings.append(subject_finding)
    warnings = [
        row["reason"]
        for key, row in analyzers.items()
        if (key.startswith("ocr_") or key == "subject_semantics")
        and row.get("status") != "analyzed"
        and row.get("reason")
    ]
    return {
        "metrics": {
            "schema_version": "reproduction-image-metrics.v2",
            "dimensions": dimensions,
            "heatmap": _heatmap(pair["diff"]),
            "available_dimension_count": sum(
                item.get("status") == "analyzed" for item in dimensions.values()
            ),
        },
        "analyzers": analyzers,
        "findings": findings,
        "warnings": warnings,
    }
