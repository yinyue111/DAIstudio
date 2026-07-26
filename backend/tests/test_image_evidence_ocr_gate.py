"""图片路径 OCR 文字门控：对齐视频路径的强覆写策略。

视频路径会用独立 OCR 轨迹覆写视觉模型的文字描述；此前图片路径只标注冲突、
不拦截。本文件验证 apply_ocr_text_gate 的五个分支：
- OCR 可信检出且一致 → confirmed；
- OCR 可信检出且冲突 → 以 OCR 为准覆写，原描述保留并写明原因；
- OCR 仅低置信检出 → 保留描述、标注 low_confidence，交人工复核；
- OCR 已分析但无任何检出 → rejected，review_status 置为 rejected；
- OCR 不可用 → 保留描述、标注 unavailable，不误杀。
"""
from __future__ import annotations

import base64
import io

from PIL import Image

from app.services import image_evidence_analysis


def _data_uri(image: Image.Image) -> str:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def _ocr_fixture(rows: list[dict], status: str = "analyzed", reason: str | None = None):
    def analyzer(_image):
        return {
            "status": status,
            "analyzer": "fixture_ocr",
            "analyzer_version": "1.0",
            "degraded_reason": reason,
            "evidence": rows,
        }
    return analyzer


def _unsupported(name):
    return lambda _image: {
        "status": "unsupported", "analyzer": name, "analyzer_version": "none",
        "degraded_reason": "fixture unavailable", "evidence": [],
    }


def _ocr_row(text: str, bbox: dict, confidence: float = 0.9) -> dict:
    return {
        "evidence_type": "ocr",
        "field_key": "ocr_text",
        "label": "visible_text",
        "evidence_text": text,
        "bbox": bbox,
        "confidence": confidence,
        "fact_status": "visible",
    }


def _vlm_text_claim(text: str, bbox: dict | None = None, confidence: float = 0.9) -> dict:
    return {
        "evidence_type": "ocr",
        "field_key": "文字版式",
        "evidence_text": text,
        "bbox": bbox,
        "confidence": confidence,
        "source_index": 1,
        "fact_status": "visible" if bbox else "inferred",
        "protected": False,
        "editable": False,
    }


def _analyze(ocr_analyzer, vlm_evidence):
    image = Image.new("RGB", (100, 100), "white")
    return image_evidence_analysis.analyze_image_sources(
        [_data_uri(image)],
        vlm_evidence=vlm_evidence,
        ocr_analyzer=ocr_analyzer,
        region_analyzer=_unsupported("proposal"),
        detector_analyzer=_unsupported("detector"),
        segmenter_analyzer=_unsupported("segmenter"),
    )


def _vlm_rows(result) -> list[dict]:
    return [
        row for row in result["evidence"]
        if row.get("analyzer_source") == "vision_language_model"
    ]


BBOX = {"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.2}


def test_conflicting_vlm_text_is_overridden_by_credible_ocr():
    result = _analyze(
        _ocr_fixture([_ocr_row("ACME PARIS", BBOX, confidence=0.93)]),
        [_vlm_text_claim("烫金法文标 Lumière", bbox=BBOX)],
    )

    row = _vlm_rows(result)[0]
    assert row["evidence_text"] == "ACME PARIS"
    assert row["vlm_text_description"] == "烫金法文标 Lumière"
    assert row["ocr_gate"]["status"] == "overridden"
    assert "已以 OCR 为准" in row["ocr_gate"]["reason"]
    assert row["ocr_gate"]["ocr_text"] == "ACME PARIS"
    assert row["ocr_gate"]["matched_evidence_ids"]
    # 覆写不是驳回：内容已被更正为像素事实，仍可进入人工复核流程。
    assert row["review_status"] == "pending"


def test_matching_vlm_text_is_confirmed_with_evidence_refs():
    result = _analyze(
        _ocr_fixture([_ocr_row("ACME PARIS", BBOX, confidence=0.93)]),
        [_vlm_text_claim("ACME", bbox=BBOX)],
    )

    row = _vlm_rows(result)[0]
    assert row["evidence_text"] == "ACME"
    assert row["ocr_gate"]["status"] == "confirmed"
    assert row["ocr_gate"]["matched_evidence_ids"]
    assert row["review_status"] == "pending"


def test_vlm_text_claim_rejected_when_analyzed_ocr_finds_nothing():
    result = _analyze(
        _ocr_fixture([]),
        [_vlm_text_claim("烫金法文标 Lumière", bbox=BBOX)],
    )

    row = _vlm_rows(result)[0]
    assert row["review_status"] == "rejected"
    assert row["ocr_gate"]["status"] == "rejected"
    assert "已拦截" in row["ocr_gate"]["reason"]
    # 原描述保留，用户能看到被拦截的是什么。
    assert row["evidence_text"] == "烫金法文标 Lumière"
    assert row["vlm_text_description"] == "烫金法文标 Lumière"


def test_vlm_text_claim_kept_and_flagged_when_ocr_unavailable():
    result = _analyze(
        _unsupported("tesseract"),
        [_vlm_text_claim("ACME", bbox=BBOX)],
    )

    row = _vlm_rows(result)[0]
    assert row["evidence_text"] == "ACME"
    assert row["review_status"] == "pending"
    assert row["ocr_gate"]["status"] == "unavailable"
    assert "无法交叉验证" in row["ocr_gate"]["reason"]


def test_degraded_ocr_is_treated_as_unavailable_with_reason():
    result = _analyze(
        _ocr_fixture([], status="degraded", reason="Tesseract 未安装配置语言: chi_sim"),
        [_vlm_text_claim("ACME", bbox=BBOX)],
    )

    row = _vlm_rows(result)[0]
    assert row["ocr_gate"]["status"] == "unavailable"
    assert "chi_sim" in row["ocr_gate"]["reason"]
    assert row["review_status"] == "pending"


def test_low_confidence_ocr_cannot_override_but_flags_for_review():
    matching = _analyze(
        _ocr_fixture([_ocr_row("LUMIERE", BBOX, confidence=0.4)]),
        [_vlm_text_claim("Lumière", bbox=BBOX)],
    )
    row = _vlm_rows(matching)[0]
    assert row["evidence_text"] == "Lumière"
    assert row["review_status"] == "pending"
    assert row["ocr_gate"]["status"] == "low_confidence"
    assert "弱验证" in row["ocr_gate"]["reason"]

    mismatched = _analyze(
        _ocr_fixture([_ocr_row("noise", BBOX, confidence=0.4)]),
        [_vlm_text_claim("烫金法文标", bbox=BBOX)],
    )
    row = _vlm_rows(mismatched)[0]
    assert row["evidence_text"] == "烫金法文标"
    assert row["review_status"] == "pending"
    assert row["ocr_gate"]["status"] == "low_confidence"
    assert "无法裁决" in row["ocr_gate"]["reason"]


def test_short_ocr_fragment_is_not_authoritative_in_single_frame():
    # 视频路径允许 "AB" 在 ≥2 帧复现且 ≥0.85 时可信；单图没有复现，
    # 0.85 仍不足 0.92 的单帧短碎片阈值，因此不能作为覆写依据。
    result = _analyze(
        _ocr_fixture([_ocr_row("AB", BBOX, confidence=0.85)]),
        [_vlm_text_claim("烫金法文标", bbox=BBOX)],
    )

    row = _vlm_rows(result)[0]
    assert row["evidence_text"] == "烫金法文标"
    assert row["ocr_gate"]["status"] == "low_confidence"

    strong = _analyze(
        _ocr_fixture([_ocr_row("AB", BBOX, confidence=0.95)]),
        [_vlm_text_claim("烫金法文标", bbox=BBOX)],
    )
    row = _vlm_rows(strong)[0]
    assert row["ocr_gate"]["status"] == "overridden"
    assert row["evidence_text"] == "AB"


def test_isolated_single_character_ocr_is_never_credible():
    result = _analyze(
        _ocr_fixture([_ocr_row("A", BBOX, confidence=0.99)]),
        [_vlm_text_claim("烫金法文标", bbox=BBOX)],
    )

    row = _vlm_rows(result)[0]
    assert row["ocr_gate"]["status"] == "low_confidence"
    assert row["evidence_text"] == "烫金法文标"


def test_credible_ocr_outside_claim_bbox_still_governs_the_claim():
    # 视觉模型的区域框画偏时，仍以整图的可信 OCR 为准，不误判成"无文字"。
    far_bbox = {"x": 0.6, "y": 0.7, "width": 0.3, "height": 0.1}
    result = _analyze(
        _ocr_fixture([_ocr_row("ACME PARIS", far_bbox, confidence=0.93)]),
        [_vlm_text_claim("烫金法文标", bbox=BBOX)],
    )

    row = _vlm_rows(result)[0]
    assert row["ocr_gate"]["status"] == "overridden"
    assert row["evidence_text"] == "ACME PARIS"


def test_region_less_vlm_text_claim_is_gated_against_whole_image():
    confirmed = _analyze(
        _ocr_fixture([_ocr_row("ACME PARIS", BBOX, confidence=0.93)]),
        [_vlm_text_claim("acme paris", bbox=None)],
    )
    assert _vlm_rows(confirmed)[0]["ocr_gate"]["status"] == "confirmed"

    rejected = _analyze(_ocr_fixture([]), [_vlm_text_claim("烫金法文标", bbox=None)])
    assert _vlm_rows(rejected)[0]["review_status"] == "rejected"


def test_non_text_vlm_evidence_is_not_gated():
    result = _analyze(
        _ocr_fixture([]),
        [{
            "evidence_type": "packaging",
            "field_key": "商品服装",
            "evidence_text": "银色矩形玻璃瓶包装",
            "bbox": BBOX,
            "confidence": 0.9,
            "source_index": 1,
            "fact_status": "visible",
            "protected": True,
            "editable": False,
        }],
    )

    row = _vlm_rows(result)[0]
    assert "ocr_gate" not in row
    assert "vlm_text_description" not in row
    assert row["review_status"] == "pending"


def test_gate_maps_ocr_status_per_source_index():
    image = Image.new("RGB", (100, 100), "white")
    calls = {"count": 0}

    def per_source_ocr(_image):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "status": "analyzed", "analyzer": "fixture_ocr",
                "analyzer_version": "1.0", "degraded_reason": None,
                "evidence": [_ocr_row("ACME", BBOX, confidence=0.93)],
            }
        return {
            "status": "unsupported", "analyzer": "fixture_ocr",
            "analyzer_version": "1.0", "degraded_reason": "第二张图不可用",
            "evidence": [],
        }

    result = image_evidence_analysis.analyze_image_sources(
        [_data_uri(image), _data_uri(image)],
        vlm_evidence=[
            _vlm_text_claim("烫金法文标", bbox=BBOX),
            {**_vlm_text_claim("第二张图上的标语", bbox=BBOX), "source_index": 2},
        ],
        ocr_analyzer=per_source_ocr,
        region_analyzer=_unsupported("proposal"),
        detector_analyzer=_unsupported("detector"),
        segmenter_analyzer=_unsupported("segmenter"),
    )

    rows = sorted(_vlm_rows(result), key=lambda row: int(row["source_index"]))
    assert rows[0]["ocr_gate"]["status"] == "overridden"
    assert rows[1]["ocr_gate"]["status"] == "unavailable"
    assert "第二张图不可用" in rows[1]["ocr_gate"]["reason"]


def test_gate_is_deterministic_across_replays():
    ocr = _ocr_fixture([_ocr_row("ACME PARIS", BBOX, confidence=0.93)])
    first = _analyze(ocr, [_vlm_text_claim("烫金法文标", bbox=BBOX)])
    second = _analyze(ocr, [_vlm_text_claim("烫金法文标", bbox=BBOX)])
    assert first["evidence"] == second["evidence"]
