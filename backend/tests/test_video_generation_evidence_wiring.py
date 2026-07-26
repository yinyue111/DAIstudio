"""视频生成入口消费反推证据链（证据门三态分镜）的接线契约。

反推分析器产出的 video_analysis（含 shot["evidence_gate"]）存放在已应用的
反推结果版本（ReverseResultRevision, source="applied"）里，客户端提交的
prompt 通常只带 final_text。本文件验证：

- /api/generate 视频请求携带反推血缘时，编译器输入被合并进证据分镜，
  最终提交给网关的提示词包含 verified 动作原文与 vlm_only 动作的
  "依据抽样帧推断"如实措辞，task.params 里留下 shot_evidence 标记；
- 存库的 task.prompt 不被改写（请求指纹与旧版一致）；
- 不带血缘的请求走旧路径，编译结果与改动前完全一致；
- Worker 的 legacy 重编译路径（_compile_legacy_video_prompt）对带血缘的
  任务同样走证据路径，对无血缘任务行为不变。
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

from app.db import SessionLocal
from app.models import GenTask, ModelConfig, ReverseOperation, ReverseResultRevision
from app.services import reverse_lineage
from app.services.generation_video_submit import (
    _compile_legacy_video_prompt,
    lineage_video_analysis_for_compile,
    merge_lineage_video_analysis,
)
from app.services.video_prompt_compiler import (
    UNVERIFIED_EVIDENCE_SUFFIX,
    compile_video_prompt,
)

VERIFIED_ACTION = "从包装下方抽出一张洗脸巾"
VLM_ONLY_ACTION = "手指捏住洗脸巾边缘缓慢展开"
FINAL_TEXT = "真实家庭浴室，产品展示广告，突出洗脸巾质感。"


def _configure_video_model(model_id: str = "grok-imagine-video-1.5") -> None:
    with SessionLocal() as db:
        model = db.query(ModelConfig).filter(ModelConfig.use == "video").one()
        model.model_id = model_id
        model.provider = "yinyue"
        model.enabled = True
        model.cost_credits = 1
        model.extra = {
            **dict(model.extra or {}),
            "prompt_profile": {
                "max_prompt_chars": 900,
                "max_shots_by_duration": {"5": 2, "10": 3, "15": 4},
            },
        }
        db.commit()


def _gate_entry(verified, confidence=None, score=None, source=None, reason=None):
    return {
        "verified": verified,
        "confidence": confidence,
        "score": score,
        "source": source,
        "reason": reason,
    }


def _gated_video_analysis() -> dict:
    return {
        "shots": [
            {
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "visual": "女主站在浴室镜前",
                "lighting": "",
                "action": VERIFIED_ACTION,
                "camera": "",
                "transition": "",
                "evidence_gate": {
                    "action": _gate_entry(
                        True, confidence="analyzer", source="semantic_provider"
                    ),
                    "camera": _gate_entry(False),
                    "transition": _gate_entry(False),
                },
            },
            {
                "start_seconds": 2.0,
                "end_seconds": 4.0,
                "visual": "微距特写洗脸巾纹理",
                "lighting": "",
                "action": VLM_ONLY_ACTION,
                "camera": "",
                "transition": "",
                "evidence_gate": {
                    "action": _gate_entry(
                        False,
                        confidence="vlm_only",
                        source="cross_frame_vlm",
                        reason="语义动作分析器不可用，动作由多帧抽样支撑但未经独立验证",
                    ),
                    "camera": _gate_entry(False),
                    "transition": _gate_entry(False),
                },
            },
        ]
    }


def _seed_video_reverse_result(
    user_id: int,
    *,
    video_analysis: dict | None = None,
) -> tuple[int, int]:
    """Seed a succeeded video reverse operation with a verified applied revision."""
    revision_payload: dict = {"final_text": FINAL_TEXT, "structured": {"主体": "洗脸巾"}}
    if video_analysis is not None:
        revision_payload["video_analysis"] = video_analysis
    with SessionLocal() as db:
        operation = ReverseOperation(
            user_id=user_id,
            request_fingerprint=hashlib.sha256(
                f"video-evidence-wiring-{user_id}".encode()
            ).hexdigest(),
            target="video",
            asset_url=f"http://example.com/reverse-video-{user_id}.mp4",
            output_purpose="generation",
            request_context={"output_purpose": "generation", "source_type": "video"},
            status="succeeded",
            progress=100,
            result=dict(revision_payload),
            normalized_result=dict(revision_payload),
        )
        db.add(operation)
        db.flush()
        fingerprint = reverse_lineage.build_source_fingerprint(
            source_index=1,
            content_hash=reverse_lineage.bytes_content_hash(
                f"reverse-video-source:{user_id}".encode()
            ),
            locator=operation.asset_url,
            method="test_source_sha256",
        )
        fingerprints = [fingerprint]
        source_hash = reverse_lineage.source_content_hash(fingerprints)
        revisions = []
        for version, revision_source, payload in (
            (1, "provider_raw", {"provider": "test", "result": revision_payload}),
            (2, "normalized", revision_payload),
            (3, "user_edit", revision_payload),
            (4, "applied", revision_payload),
        ):
            revision = ReverseResultRevision(
                operation_id=operation.id,
                user_id=user_id,
                version=version,
                source=revision_source,
                payload=dict(payload),
                parent_revision_id=(int(revisions[-1].id) if revisions else None),
                source_content_hash=source_hash,
                source_fingerprints=fingerprints,
                payload_hash=reverse_lineage.canonical_payload_hash(payload),
                lineage_status=reverse_lineage.VERIFIED,
                evidence_review_action="not_applicable",
            )
            db.add(revision)
            db.flush()
            revisions.append(revision)
        db.commit()
        return int(operation.id), int(revisions[-1].id)


def _video_payload(request_id: str, **extra) -> dict:
    return {
        "client_request_id": request_id,
        "category": "video",
        "stage": "final",
        "prompt": {"final_text": FINAL_TEXT},
        "params": {"duration": 10, "resolution": "720p", "ratio": "9:16"},
        **extra,
    }


def test_generate_with_reverse_lineage_submits_evidence_gated_motion(
    client, make_user, auth, monkeypatch, quote_and_generate
):
    user_id = make_user("13900004201", balance=1000)
    headers = auth("13900004201")
    _configure_video_model()
    operation_id, revision_id = _seed_video_reverse_result(
        user_id, video_analysis=_gated_video_analysis()
    )
    submitted = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        submitted.update(prompt=prompt, model_id=video_model_id, params=params)
        return "video-evidence-wired"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    response = quote_and_generate(
        _video_payload(
            "video-evidence-wired-001",
            reverse_operation_id=operation_id,
            reverse_revision_id=revision_id,
        ),
        headers=headers,
    )

    assert response.status_code == 200, response.text
    # verified 动作按原文写入，不带推断措辞；vlm_only 动作保留但如实标注。
    assert VERIFIED_ACTION in submitted["prompt"]
    assert f"{VERIFIED_ACTION}{UNVERIFIED_EVIDENCE_SUFFIX}" not in submitted["prompt"]
    assert f"{VLM_ONLY_ACTION}{UNVERIFIED_EVIDENCE_SUFFIX}" in submitted["prompt"]

    with SessionLocal() as db:
        task = db.get(GenTask, response.json()["id"])
        params = task.params or {}
        assert params["_generation_prompt"] == submitted["prompt"]
        evidence = params["_video_shot_evidence"]
        assert len(evidence) == 2
        assert evidence[0]["action"]["verified"] is True
        assert evidence[0]["action"]["source"] == "semantic_provider"
        assert evidence[1]["action"]["verified"] is False
        assert evidence[1]["action"]["confidence"] == "vlm_only"
        # 编译输入的合并不改写存库 prompt：请求指纹与旧版保持一致。
        assert "video_analysis" not in (task.prompt or {})
        assert task.reverse_operation_id == operation_id
        assert task.source_revision_id == revision_id


def test_generate_without_lineage_keeps_legacy_compile_output(
    client, make_user, auth, monkeypatch, quote_and_generate
):
    make_user("13900004202", balance=1000)
    headers = auth("13900004202")
    _configure_video_model()
    submitted = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        submitted.update(prompt=prompt, model_id=video_model_id, params=params)
        return "video-evidence-absent"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    response = quote_and_generate(
        _video_payload("video-evidence-absent-001"),
        headers=headers,
    )

    assert response.status_code == 200, response.text
    # 与改动前逐字一致：直接用旧输入调用编译器得到的就是提交的提示词。
    baseline = compile_video_prompt(
        {"final_text": FINAL_TEXT},
        duration=10,
        model_id="grok-imagine-video-1.5",
        provider="yinyue",
        extra={
            "prompt_profile": {
                "max_prompt_chars": 900,
                "max_shots_by_duration": {"5": 2, "10": 3, "15": 4},
            }
        },
        references=[],
        fit_mode="single_clip",
    )
    assert submitted["prompt"] == baseline["prompt"]
    assert VERIFIED_ACTION not in submitted["prompt"]
    assert UNVERIFIED_EVIDENCE_SUFFIX not in submitted["prompt"]

    with SessionLocal() as db:
        task = db.get(GenTask, response.json()["id"])
        assert (task.params or {})["_video_shot_evidence"] == []


def test_lineage_without_gated_shots_never_changes_compiler_input():
    # 老分析结果没有 evidence_gate：提取器返回 None，合并是恒等操作。
    assert lineage_video_analysis_for_compile(None) is None
    assert lineage_video_analysis_for_compile({"final_text": "x"}) is None
    assert (
        lineage_video_analysis_for_compile(
            {"video_analysis": {"shots": [{"visual": "无证据门分镜"}]}}
        )
        is None
    )
    prompt = {"final_text": FINAL_TEXT}
    assert merge_lineage_video_analysis(prompt, None) is prompt
    # 字符串提示词（direct/旧任务）原样返回。
    assert merge_lineage_video_analysis(FINAL_TEXT, _gated_video_analysis()) == FINAL_TEXT
    # 客户端已显式携带 video_analysis 时不覆盖。
    explicit = {"final_text": FINAL_TEXT, "video_analysis": {"shots": []}}
    assert merge_lineage_video_analysis(explicit, _gated_video_analysis()) is explicit
    # 合并返回浅拷贝，原 dict 不被修改。
    merged = merge_lineage_video_analysis(prompt, _gated_video_analysis())
    assert "video_analysis" in merged
    assert "video_analysis" not in prompt


def test_legacy_worker_recompile_walks_evidence_path_for_lineage_tasks(
    client, make_user
):
    user_id = make_user("13900004203", balance=1000)
    operation_id, revision_id = _seed_video_reverse_result(
        user_id, video_analysis=_gated_video_analysis()
    )
    task = SimpleNamespace(
        prompt={"final_text": FINAL_TEXT},
        source_asset_url=None,
        source_type=None,
        stage="final",
        user_id=user_id,
        reverse_operation_id=operation_id,
        source_revision_id=revision_id,
    )
    model = SimpleNamespace(
        model_id="grok-imagine-video-1.5",
        provider="yinyue",
        extra={},
    )
    with SessionLocal() as db:
        prompt, persisted = _compile_legacy_video_prompt(
            task, model, {"duration": 10}, db=db
        )
    assert VERIFIED_ACTION in prompt
    assert f"{VLM_ONLY_ACTION}{UNVERIFIED_EVIDENCE_SUFFIX}" in prompt
    assert persisted["_video_shot_evidence"][0]["action"]["verified"] is True
    # 编译输入合并不回写 task.prompt。
    assert "video_analysis" not in task.prompt


def test_legacy_worker_recompile_unchanged_without_lineage(client):
    task = SimpleNamespace(
        prompt={"final_text": FINAL_TEXT},
        source_asset_url=None,
        source_type=None,
        stage="final",
    )
    model = SimpleNamespace(
        model_id="grok-imagine-video-1.5",
        provider="yinyue",
        extra={},
    )
    # 旧调用形态（不传 db）仍然可用，行为与改动前一致。
    prompt, persisted = _compile_legacy_video_prompt(task, model, {"duration": 10})
    baseline = compile_video_prompt(
        {"final_text": FINAL_TEXT},
        duration=10,
        model_id="grok-imagine-video-1.5",
        provider="yinyue",
        extra={},
        references=[],
        fit_mode="single_clip",
    )
    assert prompt == baseline["prompt"]
    assert persisted["_video_shot_evidence"] == []
