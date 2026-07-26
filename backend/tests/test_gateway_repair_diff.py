"""反推文本修复的事实差分校验。

修复请求的指令是"只负责修复 JSON 结构,不得增加、删除或改写事实",
但模型返回的是整份文档。这里验证 _enforce_repair_shot_facts 与
_repair_reverse_result_once 能拦住偷改:单处改写回滚并告警,
大面积改写整体失败,老实修复原样通过。
"""

from __future__ import annotations

import copy
import json

import pytest

from app.services import gateway
from app.services.gateway import (
    GatewayError,
    _enforce_repair_shot_facts,
    _repair_reverse_result_once,
)
from app.services.gateway_prompting import ReverseResultValidationError


def _original_payload() -> dict:
    return {
        "主体": "黑色玻璃瓶精华液",
        "shots": [
            {
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "visual": "黑场中产品轮廓被冷白光勾勒",
                "camera": "镜头缓慢推进",
                "evidence_frame_indices": [1],
                "confidence": 0.9,
            },
            {
                "start_seconds": 2.0,
                "end_seconds": 4.0,
                "visual": "银色滴管盖微距特写",
                "action": "液滴落入瓶口",
                "evidence_frame_indices": [2],
                "confidence": 0.9,
            },
        ],
        "final_text": "供应商自由文本",
    }


def _original_content() -> str:
    return json.dumps(_original_payload(), ensure_ascii=False)


class TestEnforceRepairShotFacts:
    def test_honest_structural_repair_passes_untouched(self):
        repaired = _original_payload()
        payload, warnings = _enforce_repair_shot_facts(_original_content(), repaired)

        assert warnings == []
        assert [shot["visual"] for shot in payload["shots"]] == [
            "黑场中产品轮廓被冷白光勾勒",
            "银色滴管盖微距特写",
        ]

    def test_single_rewritten_shot_is_rolled_back_with_warning(self):
        repaired = _original_payload()
        # 模型把静止镜头偷偷改写成环绕推近
        repaired["shots"][0]["camera"] = "镜头快速环绕推近"

        payload, warnings = _enforce_repair_shot_facts(_original_content(), repaired)

        assert payload["shots"][0]["camera"] == "镜头缓慢推进"
        assert len(warnings) == 1
        assert warnings[0]["reason"] == "fields_rewritten"
        assert warnings[0]["fields"] == ["camera"]
        assert warnings[0]["evidence_frame_indices"] == [1]

    def test_rewritten_timestamps_are_rolled_back(self):
        repaired = _original_payload()
        repaired["shots"][1]["start_seconds"] = 10.0

        payload, warnings = _enforce_repair_shot_facts(_original_content(), repaired)

        assert payload["shots"][1]["start_seconds"] == 2.0
        assert warnings[0]["fields"] == ["start_seconds"]

    def test_deleted_shot_is_restored(self):
        repaired = _original_payload()
        # 模型删掉了帧 2 的 shot
        repaired["shots"] = repaired["shots"][:1]

        payload, warnings = _enforce_repair_shot_facts(_original_content(), repaired)

        evidences = [shot["evidence_frame_indices"] for shot in payload["shots"]]
        assert [1] in evidences
        assert [2] in evidences
        assert [warning["reason"] for warning in warnings] == ["shot_deleted"]

    def test_added_shot_is_dropped(self):
        repaired = _original_payload()
        # 模型凭空捏了一个帧 3 的 shot
        repaired["shots"].append({
            "start_seconds": 4.0,
            "end_seconds": 6.0,
            "visual": "凭空捏造的画面",
            "evidence_frame_indices": [3],
            "confidence": 0.9,
        })

        payload, warnings = _enforce_repair_shot_facts(_original_content(), repaired)

        evidences = [shot["evidence_frame_indices"] for shot in payload["shots"]]
        assert [1] in evidences
        assert [2] in evidences
        assert [3] not in evidences
        assert [warning["reason"] for warning in warnings] == ["shot_added"]

    def test_delete_plus_fabricate_on_small_baseline_fails(self):
        # 删一个再捏一个 = 2 处违规,已超过 2 个 shot 基线的一半 -> 整体失败
        repaired = _original_payload()
        repaired["shots"] = [
            repaired["shots"][0],
            {
                "start_seconds": 4.0,
                "end_seconds": 6.0,
                "visual": "凭空捏造的画面",
                "evidence_frame_indices": [3],
                "confidence": 0.9,
            },
        ]

        with pytest.raises(GatewayError, match="未遵守指令"):
            _enforce_repair_shot_facts(_original_content(), repaired)

    def test_majority_rewrite_fails_whole_repair(self):
        repaired = _original_payload()
        repaired["shots"][0]["visual"] = "改写后的画面一"
        repaired["shots"][1]["visual"] = "改写后的画面二"

        with pytest.raises(GatewayError) as exc_info:
            _enforce_repair_shot_facts(_original_content(), repaired)

        assert exc_info.value.error_code == "INVALID_REVERSE_RESULT"
        assert exc_info.value.phase == "repairing"
        assert "未遵守指令" in str(exc_info.value)

    def test_single_violation_on_single_shot_baseline_still_salvaged(self):
        # 只有 1 个 shot 时单处违规按回滚放行,不浪费一次成功的修复
        original = _original_payload()
        original["shots"] = original["shots"][:1]
        repaired = copy.deepcopy(original)
        repaired["shots"][0]["visual"] = "改写后的画面"

        payload, warnings = _enforce_repair_shot_facts(
            json.dumps(original, ensure_ascii=False), repaired
        )

        assert payload["shots"][0]["visual"] == "黑场中产品轮廓被冷白光勾勒"
        assert len(warnings) == 1

    def test_undecodable_original_skips_diff(self):
        repaired = _original_payload()
        payload, warnings = _enforce_repair_shot_facts("这不是 JSON", repaired)

        assert warnings == []
        assert payload is repaired

    def test_invalid_typed_original_field_allows_structural_fix(self):
        # 原值类型无效(结构错误)时允许修复纠正,不算改写事实
        original = _original_payload()
        original["shots"][0]["start_seconds"] = "0.0秒"
        repaired = _original_payload()

        payload, warnings = _enforce_repair_shot_facts(
            json.dumps(original, ensure_ascii=False), repaired
        )

        assert warnings == []
        assert payload["shots"][0]["start_seconds"] == 0.0


class TestRepairReverseResultOnce:
    """端到端:模拟网关返回,验证差分校验挂在文本修复链路上。"""

    def _invalid_original_content(self) -> str:
        payload = _original_payload()
        # 非法置信度触发严格校验失败 -> 进入文本修复
        payload["shots"][0]["confidence"] = 1.5
        return json.dumps(payload, ensure_ascii=False)

    def _run_repair(self, monkeypatch, repaired_payload: dict) -> dict:
        def fake_post(path, payload, **kwargs):
            return {
                "choices": [{
                    "message": {
                        "content": json.dumps(repaired_payload, ensure_ascii=False),
                    },
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            }

        monkeypatch.setattr(gateway, "_post", fake_post)
        content = self._invalid_original_content()
        try:
            gateway._validate_reverse_result(content, "video")
            raise AssertionError("原始内容应当校验失败以触发修复")
        except ReverseResultValidationError as error:
            result, usage = _repair_reverse_result_once(
                content,
                error,
                target="video",
                vision_model_id="test-vision",
                gateway_config=None,
            )
        assert usage is not None
        return result

    def test_honest_repair_passes_without_rollback(self, monkeypatch):
        repaired = _original_payload()
        repaired["shots"][0]["confidence"] = 0.9  # 只修结构错误

        result = self._run_repair(monkeypatch, repaired)

        assert "repair_fact_rollbacks" not in result
        assert [shot["visual"] for shot in result["shots"]] == [
            "黑场中产品轮廓被冷白光勾勒",
            "银色滴管盖微距特写",
        ]

    def test_sneaky_rewrite_is_rolled_back_and_flagged(self, monkeypatch):
        repaired = _original_payload()
        repaired["shots"][0]["confidence"] = 0.9
        repaired["shots"][0]["camera"] = "镜头快速环绕推近"

        result = self._run_repair(monkeypatch, repaired)

        assert result["shots"][0]["camera"] == "镜头缓慢推进"
        rollbacks = result["repair_fact_rollbacks"]
        assert len(rollbacks) == 1
        assert rollbacks[0]["reason"] == "fields_rewritten"
        assert rollbacks[0]["fields"] == ["camera"]

    def test_wholesale_rewrite_fails_repair(self, monkeypatch):
        repaired = _original_payload()
        repaired["shots"][0]["confidence"] = 0.9
        repaired["shots"][0]["visual"] = "改写后的画面一"
        repaired["shots"][1]["visual"] = "改写后的画面二"

        with pytest.raises(GatewayError) as exc_info:
            self._run_repair(monkeypatch, repaired)

        assert exc_info.value.error_code == "INVALID_REVERSE_RESULT"
        assert "未遵守指令" in str(exc_info.value)

    def test_undecodable_repair_response_still_fails_validation(self, monkeypatch):
        def fake_post(path, payload, **kwargs):
            return {"choices": [{"message": {"content": "仍然不是 JSON"}}]}

        monkeypatch.setattr(gateway, "_post", fake_post)
        content = self._invalid_original_content()
        try:
            gateway._validate_reverse_result(content, "video")
            raise AssertionError("原始内容应当校验失败以触发修复")
        except ReverseResultValidationError as error:
            with pytest.raises(GatewayError) as exc_info:
                _repair_reverse_result_once(
                    content,
                    error,
                    target="video",
                    vision_model_id="test-vision",
                    gateway_config=None,
                )
        assert "一次自动修复后仍无效" in str(exc_info.value)
