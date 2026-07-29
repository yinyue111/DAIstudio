"""Real-cost accounting (gateway_calls) + video keyframe graceful fallback."""
import csv
import io
from datetime import datetime, timezone
from types import SimpleNamespace

from app.db import SessionLocal
from app.models import (
    AuditLog,
    CreationRecipe,
    CreditTransaction,
    GatewayCall,
    GenTask,
    ReverseOperation,
    ReverseOperationFeedback,
    ReverseResultRevision,
    User,
)
from app.routers.admin_usage import _evidence_coverage
from app.services import audit, credits, gateway, usage, video_analysis, video_frames


def test_reverse_logs_gateway_call(client, make_user, auth):
    make_user("13900000030", balance=1000)
    h = auth("13900000030")
    r = client.post("/api/prompt/reverse",
                    json={"asset_url": "http://x/y.png", "target": "image"}, headers=h)
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        rows = db.query(GatewayCall).filter(GatewayCall.kind == "reverse").all()
        assert rows, "a reverse call must be logged for real-cost accounting"
    finally:
        db.close()


def test_reverse_uses_fixed_operation_price(client, make_user, auth):
    uid = make_user("13900000035", balance=1000, admin=True)
    h = auth("13900000035")

    r = client.put("/api/admin/models", json={
        "use": "vision",
        "model_id": "mock-vision",
        "cost_credits": 7,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 200, r.text

    r = client.post("/api/prompt/reverse",
                    json={"asset_url": "http://x/y.png", "target": "image"}, headers=h)
    assert r.status_code == 200, r.text

    assert client.get("/api/me", headers=h).json()["balance_credits"] == 995
    db = SessionLocal()
    try:
        transactions = db.query(CreditTransaction).filter(
            CreditTransaction.user_id == uid,
            CreditTransaction.biz_type == "reverse_operation",
        ).order_by(CreditTransaction.id).all()
        assert [(tx.type, tx.change) for tx in transactions] == [
            ("freeze", -5),
            ("settle", 0),
        ]
        assert transactions[-1].reserved_amount == 5
        assert transactions[-1].real_cost == 5
    finally:
        db.close()


def test_reverse_refunds_configured_cost_on_gateway_failure(client, make_user, auth, monkeypatch):
    make_user("13900000036", balance=1000, admin=True)
    h = auth("13900000036")

    r = client.put("/api/admin/models", json={
        "use": "vision",
        "model_id": "mock-vision",
        "cost_credits": 7,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 200, r.text

    def fail(*_args, **_kwargs):
        raise gateway.GatewayError("vision failed")

    monkeypatch.setattr("app.services.gateway.reverse_prompt", fail)
    r = client.post("/api/prompt/reverse",
                    json={"asset_url": "http://x/y.png", "target": "image"}, headers=h)
    assert r.status_code == 502
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 1000


def test_reverse_refunds_configured_cost_on_unexpected_gateway_error(client, make_user, auth, monkeypatch):
    make_user("13900009040", balance=1000, admin=True)
    h = auth("13900009040")

    r = client.put("/api/admin/models", json={
        "use": "vision",
        "model_id": "mock-vision",
        "cost_credits": 7,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 200, r.text

    def fail(*_args, **_kwargs):
        raise RuntimeError("bad response shape")

    monkeypatch.setattr("app.services.gateway.reverse_prompt", fail)
    r = client.post("/api/prompt/reverse",
                    json={"asset_url": "http://x/y.png", "target": "image"}, headers=h)
    assert r.status_code == 502
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 1000


def test_admin_report_includes_reverse_model_call_spend(client, make_user, auth):
    make_user("13900000037", balance=1000, admin=True)
    h = auth("13900000037")

    r = client.put("/api/admin/models", json={
        "use": "vision",
        "model_id": "mock-vision",
        "cost_credits": 7,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 200, r.text
    r = client.post("/api/prompt/reverse",
                    json={"asset_url": "http://x/y.png", "target": "image"}, headers=h)
    assert r.status_code == 200, r.text

    report = client.get("/api/admin/usage/report", headers=h).json()
    row = next(x for x in report["per_user"] if x["phone"] == "13900000037")
    assert row["spend_credits"] == 5


def test_admin_report_and_reverse_dashboard_include_async_settlement(
    client,
    make_user,
    auth,
):
    phone = "13900000038"
    uid = make_user(phone, balance=1000, admin=True)
    headers = auth(phone)
    now = datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-usage-0001",
            request_fingerprint="a" * 64,
            target="video",
            asset_url="http://x/reference.mp4",
            status="succeeded",
            phase=None,
            progress=100,
            request_context={
                "cover_confirmation_required": True,
                "cover_confirmed": True,
            },
            result={
                "structured": {"主体": "产品"},
                "final_text": "产品运动提示词",
                "repair_attempted": True,
                "video_analysis": {
                    "analysis_mode": "cover_fallback",
                    "source": {"duration_seconds": 10, "audio_analyzed": False},
                    "analysis_gaps": [
                        {"start_seconds": 8, "end_seconds": 10},
                        {"start_seconds": 9, "end_seconds": 10},
                    ],
                },
            },
            charged_credits=2,
            cost_frozen=0,
            cost_settled=2,
            reference_count=1,
            started_at=now,
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(operation)
        db.flush()
        credits.consume(
            db,
            uid,
            2,
            biz_type="reverse_operation",
            biz_ref=operation.id,
            note="compatibility transaction that must not be counted twice",
            commit=False,
        )
        db.commit()
    finally:
        db.close()

    date_filter = "?start=2099-01-01&end=2099-01-01"
    report = client.get(f"/api/admin/usage/report{date_filter}", headers=headers)
    assert report.status_code == 200, report.text
    user_row = next(row for row in report.json()["per_user"] if row["phone"] == phone)
    assert user_row["spend_credits"] == 2
    assert user_row["tasks"]["reverse"] == 1

    dashboard = client.get(
        f"/api/admin/usage/reverse-operations{date_filter}",
        headers=headers,
    )
    assert dashboard.status_code == 200, dashboard.text
    body = dashboard.json()
    assert body["summary"]["succeeded"] == 1
    assert body["summary"]["settled_credits"] == 2
    assert body["summary"]["failure_rate"] == 0
    assert body["summary"]["cancel_rate"] == 0
    assert body["quality"]["cover_fallback_rate"] == 1
    assert body["quality"]["cover_confirmation_required_count"] == 1
    assert body["quality"]["cover_confirmed_count"] == 1
    assert body["quality"]["cover_confirmation_rate"] == 1
    assert body["quality"]["repair_rate"] == 1
    assert body["quality"]["repair_succeeded_count"] == 1
    assert body["quality"]["repair_failed_count"] == 0
    assert body["quality"]["avg_evidence_coverage"] == 0.8
    assert body["by_preset"] == [{
        "preset": "standard",
        "operation_count": 1,
        "succeeded": 1,
        "settled_credits": 2,
    }]
    assert body["by_target"] == [{
        "target": "video",
        "operation_count": 1,
        "succeeded": 1,
        "settled_credits": 2,
    }]


def test_reverse_dashboard_counts_pending_confirmation_and_failed_repair(
    client,
    make_user,
    auth,
):
    phone = "13900000039"
    uid = make_user(phone, balance=1000, admin=True)
    headers = auth(phone)
    now = datetime(2099, 2, 1, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        confirmed = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-quality-confirmed",
            request_fingerprint="b" * 64,
            target="video",
            asset_url="http://x/confirmed.mp4",
            status="succeeded",
            progress=100,
            request_context={
                "cover_confirmation_required": True,
                "cover_confirmed": True,
            },
            result={
                "repair_attempted": True,
                "repair_succeeded": True,
                "video_analysis": {"analysis_mode": "cover_fallback"},
            },
            cost_frozen=0,
            cost_settled=2,
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        declined = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-quality-declined",
            request_fingerprint="c" * 64,
            target="video",
            asset_url="http://x/declined.mp4",
            status="canceled",
            progress=100,
            request_context={"cover_confirmation_required": True},
            result={"video_analysis": {"analysis_mode": "unavailable"}},
            cost_frozen=0,
            cost_settled=0,
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        failed_repair = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-quality-repair-failed",
            request_fingerprint="d" * 64,
            target="image",
            asset_url="http://x/failed.png",
            status="failed",
            progress=100,
            cost_frozen=0,
            cost_settled=0,
            error_code="REPAIR_FAILED",
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add_all([confirmed, declined, failed_repair])
        db.flush()
        db.add(GatewayCall(
            user_id=uid,
            kind="reverse",
            model_id="mock-vision",
            status="failed",
            detail={
                "operation_id": failed_repair.id,
                "phase": "repairing",
                "error_code": "REPAIR_FAILED",
                "repair_attempted": True,
                "cost_credits": 0,
            },
            created_at=now,
        ))
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/admin/usage/reverse-operations?start=2099-02-01&end=2099-02-01",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    quality = response.json()["quality"]
    assert quality["cover_confirmation_required_count"] == 2
    assert quality["cover_confirmed_count"] == 1
    assert quality["cover_confirmation_rate"] == 0.5
    assert quality["repair_count"] == 2
    assert quality["repair_succeeded_count"] == 1
    assert quality["repair_failed_count"] == 1
    assert quality["repair_rate"] == 0.6667


def test_reverse_dashboard_attributes_operation_and_model_cost_to_finished_date(
    client,
    make_user,
    auth,
):
    phone = "13900000040"
    uid = make_user(phone, balance=1000, admin=True)
    headers = auth(phone)
    created_at = datetime(2099, 3, 1, 23, 59, tzinfo=timezone.utc)
    finished_at = datetime(2099, 3, 2, 0, 1, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-cross-day-reporting",
            request_fingerprint="e" * 64,
            target="image",
            asset_url="http://x/cross-day.png",
            status="succeeded",
            progress=100,
            result={"structured": {"subject": "cup"}, "final_text": "red cup"},
            cost_frozen=0,
            cost_settled=2,
            started_at=created_at,
            finished_at=finished_at,
            created_at=created_at,
            updated_at=finished_at,
        )
        db.add(operation)
        db.flush()
        db.add(GatewayCall(
            user_id=uid,
            kind="reverse",
            model_id="mock-vision",
            status="ok",
            detail={"operation_id": operation.id, "cost_credits": 3},
            created_at=created_at,
        ))
        db.commit()
    finally:
        db.close()

    first_day = client.get(
        "/api/admin/usage/reverse-operations?start=2099-03-01&end=2099-03-01",
        headers=headers,
    ).json()
    second_day = client.get(
        "/api/admin/usage/reverse-operations?start=2099-03-02&end=2099-03-02",
        headers=headers,
    ).json()

    assert first_day["summary"]["operation_count"] == 0
    assert first_day["summary"]["settled_credits"] == 0
    assert first_day["model_costs"] == []
    assert second_day["summary"]["operation_count"] == 1
    assert second_day["summary"]["settled_credits"] == 2
    assert second_day["model_costs"] == [{
        "model_id": "mock-vision",
        "call_count": 1,
        "failed_count": 0,
        "total_tokens": 0,
        "cost_credits": 3,
    }]


def test_reverse_dashboard_reports_adoption_edit_generation_feedback_and_recipe(
    client,
    make_user,
    auth,
):
    phone = "13900000042"
    uid = make_user(phone, balance=1000, admin=True)
    headers = auth(phone)
    now = datetime(2099, 4, 1, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-quality-funnel",
            request_fingerprint="f" * 64,
            target="image",
            analysis_focus="product_ad",
            asset_url="http://x/product.png",
            status="succeeded",
            progress=100,
            request_context={"source_type": "image"},
            model_snapshot={"model_id": "vision-quality"},
            result={"structured": {"subject": "cup"}, "final_text": "red cup"},
            cost_frozen=0,
            cost_settled=3,
            started_at=now,
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(operation)
        db.flush()
        db.add_all([
            ReverseResultRevision(
                operation_id=operation.id,
                user_id=uid,
                version=1,
                source="normalized",
                payload={"final_text": "red cup"},
            ),
            ReverseResultRevision(
                operation_id=operation.id,
                user_id=uid,
                version=2,
                source="user_edit",
                payload={"final_text": "red ceramic cup"},
            ),
            ReverseResultRevision(
                operation_id=operation.id,
                user_id=uid,
                version=3,
                source="applied",
                payload={"final_text": "red ceramic cup", "changed_fields": ["prompt"]},
            ),
            ReverseResultRevision(
                operation_id=operation.id,
                user_id=uid,
                version=4,
                source="generation",
                payload={"final_text": "red ceramic cup", "generation": {"task_id": 99}},
            ),
            ReverseOperationFeedback(
                operation_id=operation.id,
                user_id=uid,
                rating="useful",
                issue_types=["text_error"],
                note="fixed before generation",
            ),
            CreationRecipe(
                user_id=uid,
                source_operation_id=operation.id,
                title="product recipe",
                category="image",
                visibility="private",
                current_version=1,
            ),
        ])
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/admin/usage/reverse-operations?start=2099-04-01&end=2099-04-01",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    quality = body["quality"]
    assert quality["feedback_count"] == 1
    assert quality["useful_rate"] == 1
    assert quality["adopted_operation_count"] == 1
    assert quality["adoption_rate"] == 1
    assert quality["edited_operation_count"] == 1
    assert quality["edit_rate"] == 1
    assert 0 < quality["avg_edit_ratio"] < 1
    assert quality["generated_operation_count"] == 1
    assert quality["generation_conversion_rate"] == 1
    assert quality["recipe_operation_count"] == 1
    assert quality["recipe_conversion_rate"] == 1
    assert quality["issue_types"] == {"text_error": 1}
    assert body["by_focus"][0]["focus"] == "product_ad"
    assert body["by_focus"][0]["adoption_rate"] == 1
    assert body["by_media_type"][0]["media_type"] == "image"
    assert body["by_model_quality"][0]["model"] == "vision-quality"


def test_reverse_quality_report_filters_dimensions_and_reports_economics(
    client,
    make_user,
    auth,
):
    phone = "13900000043"
    uid = make_user(phone, balance=1000, admin=True)
    headers = auth(phone)
    now = datetime(2099, 5, 1, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        image = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-quality-image-filter",
            request_fingerprint="1" * 64,
            target="image",
            analysis_focus="product_ad",
            asset_url="http://x/product.png",
            status="succeeded",
            progress=100,
            request_context={"source_type": "image"},
            model_snapshot={"model_id": "=vision-image"},
            result={
                "final_text": "red cup",
                "image_evidence_analyzers": {
                    "ocr": [{"status": "analyzed"}],
                    "region_proposal": [{"status": "analyzed"}],
                    "detector": [{"status": "analyzed"}],
                    "segmenter": [{"status": "unsupported"}],
                },
            },
            cost_frozen=0,
            cost_settled=10,
            started_at=now,
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        video = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-quality-video-filter",
            request_fingerprint="2" * 64,
            target="video",
            analysis_focus="storyboard",
            asset_url="http://x/story.mp4",
            status="failed",
            progress=100,
            request_context={"source_type": "video"},
            model_snapshot={"model_id": "vision-video"},
            result={
                "video_analysis": {
                    "source": {"duration_seconds": 10},
                    "analysis_gaps": [{"start_seconds": 5, "end_seconds": 10}],
                },
            },
            cost_frozen=0,
            cost_settled=0,
            started_at=now,
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add_all([image, video])
        db.flush()
        db.add_all([
            ReverseResultRevision(
                operation_id=image.id,
                user_id=uid,
                version=1,
                source="normalized",
                payload={"final_text": "red cup"},
            ),
            ReverseResultRevision(
                operation_id=image.id,
                user_id=uid,
                version=2,
                source="user_edit",
                payload={"final_text": "red ceramic cup"},
            ),
            ReverseResultRevision(
                operation_id=image.id,
                user_id=uid,
                version=3,
                source="applied",
                payload={"final_text": "red ceramic cup"},
            ),
            ReverseResultRevision(
                operation_id=image.id,
                user_id=uid,
                version=4,
                source="generation",
                payload={"final_text": "red ceramic cup"},
            ),
            ReverseOperationFeedback(
                operation_id=image.id,
                user_id=uid,
                rating="useful",
                issue_types=[],
            ),
            CreationRecipe(
                user_id=uid,
                source_operation_id=image.id,
                title="filtered recipe",
                category="image",
                visibility="private",
                current_version=1,
            ),
            GatewayCall(
                user_id=uid,
                kind="reverse",
                model_id="=vision-image",
                status="ok",
                detail={
                    "operation_id": image.id,
                    "provider_cost_status": "complete",
                    "provider_cost_credits": 4,
                    "cost_credits": 10,
                },
                created_at=now,
            ),
            GatewayCall(
                user_id=uid,
                kind="reverse",
                model_id="vision-video",
                status="failed",
                detail={
                    "operation_id": video.id,
                    "provider_cost_status": "complete",
                    "provider_cost_credits": 2,
                    "cost_credits": 8,
                },
                created_at=now,
            ),
        ])
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/admin/usage/reverse-operations",
        params={
            "media_type": "image",
            "model": "=vision-image",
            "focus": "product_ad",
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filters"]["selected"] == {
        "media_type": "image",
        "model": "=vision-image",
        "focus": "product_ad",
    }
    assert {"image", "video"}.issubset(body["filters"]["options"]["media_types"])
    assert {"=vision-image", "vision-video"}.issubset(
        body["filters"]["options"]["models"]
    )
    assert {"product_ad", "storyboard"}.issubset(
        body["filters"]["options"]["focuses"]
    )
    assert body["summary"]["operation_count"] == 1
    assert body["summary"]["success_rate"] == 1
    assert body["quality"]["avg_evidence_coverage"] == 0.75
    assert body["quality"]["adoption_rate"] == 1
    assert body["quality"]["edit_rate"] == 1
    assert body["quality"]["generation_conversion_rate"] == 1
    assert body["quality"]["recipe_conversion_rate"] == 1
    assert body["economics"] == {
        "basis": "settled_credits_minus_provider_cost_snapshot",
        "cost_status": "complete",
        "revenue_credits": 10,
        "provider_cost_credits": 4,
        "attributed_provider_cost_credits": 4,
        "unattributed_provider_cost_credits": 0,
        "gross_profit_credits": 6,
        "gross_margin_rate": 0.6,
        "gateway_call_count": 1,
        "gateway_cost_record_count": 1,
        "gateway_cost_coverage_rate": 1.0,
        "operation_cost_coverage_rate": 1.0,
    }
    image_row = body["by_media_type"][0]
    assert image_row["media_type"] == "image"
    assert image_row["avg_evidence_coverage"] == 0.75
    assert image_row["avg_edit_ratio"] > 0
    assert image_row["provider_cost_credits"] == 4
    assert image_row["cost_status"] == "complete"
    assert image_row["gross_profit_credits"] == 6
    assert image_row["gross_margin_rate"] == 0.6
    assert image_row["cost_coverage_rate"] == 1.0


def test_reverse_quality_csv_matches_filters_and_escapes_formula_values(
    client,
    make_user,
    auth,
):
    phone = "13900000044"
    uid = make_user(phone, balance=1000, admin=True)
    headers = auth(phone)
    now = datetime(2099, 5, 2, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-quality-csv",
            request_fingerprint="3" * 64,
            target="image",
            analysis_focus="product_ad",
            asset_url="http://x/csv.png",
            status="succeeded",
            progress=100,
            request_context={"source_type": "image"},
            model_snapshot={"model_id": "=csv-formula"},
            result={"final_text": "csv"},
            cost_frozen=0,
            cost_settled=5,
            started_at=now,
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(operation)
        db.flush()
        db.add(GatewayCall(
            user_id=uid,
            kind="reverse",
            model_id="=csv-formula",
            status="ok",
            detail={
                "operation_id": operation.id,
                "provider_cost_status": "complete",
                "provider_cost_credits": 2,
                "cost_credits": 5,
            },
            created_at=now,
        ))
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/admin/usage/reverse-operations",
        params={"model": "=csv-formula", "format": "csv"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "reverse_quality_report.csv" in response.headers["content-disposition"]
    assert response.content.startswith("\ufeff".encode())
    text = response.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("dimension,value,operation_count")
    assert "model,'=csv-formula,1,1,1.0" in text
    assert "media_type,video" not in text
    rows = list(csv.DictReader(io.StringIO(text)))
    model_row = next(row for row in rows if row["dimension"] == "model")
    assert model_row["cost_status"] == "complete"
    assert model_row["provider_cost_credits"] == "2"
    assert model_row["gross_profit_credits"] == "3"


def test_video_evidence_coverage_uses_selected_range_union_and_clips_gaps():
    selected = SimpleNamespace(
        result={
            "video_analysis": {
                "source": {"duration_seconds": 10},
                "analysis_gaps": [
                    {"start_seconds": 2, "end_seconds": 5},
                    {"start_seconds": 4, "end_seconds": 9},
                ],
            }
        },
        request_context={
            "source_ranges": [{"start_seconds": 0, "end_seconds": 10}]
        },
        source_ranges=[
            {"start_seconds": 0, "end_seconds": 4},
            {"start_seconds": 3, "end_seconds": 6},
            {"start_seconds": 8, "end_seconds": 10},
        ],
        source_range=None,
    )
    assert _evidence_coverage(selected) == 0.375

    no_selection = SimpleNamespace(
        result={
            "video_analysis": {
                "source": {"duration_seconds": 10},
                "analysis_gaps": [{"start_seconds": 2, "end_seconds": 4}],
            }
        },
        request_context={},
        source_ranges=[],
        source_range=None,
    )
    assert _evidence_coverage(no_selection) == 0.8

    invalid_selection = SimpleNamespace(
        result={
            "video_analysis": {
                "source": {"duration_seconds": 10},
                "analysis_gaps": [],
            }
        },
        request_context={
            "source_ranges": [{"start_seconds": 0, "end_seconds": 5}]
        },
        source_ranges=[
            {"start_seconds": 11, "end_seconds": 12},
            {"start_seconds": "invalid", "end_seconds": 5},
        ],
        source_range=None,
    )
    assert _evidence_coverage(invalid_selection) is None


def test_reverse_quality_does_not_treat_user_charge_as_provider_cost(
    client,
    make_user,
    auth,
):
    phone = "13900000045"
    uid = make_user(phone, balance=1000, admin=True)
    headers = auth(phone)
    now = datetime(2099, 5, 3, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-provider-cost-legacy-only",
            request_fingerprint="4" * 64,
            target="image",
            analysis_focus="provider_cost_legacy",
            asset_url="http://x/legacy-cost.png",
            status="succeeded",
            progress=100,
            request_context={"source_type": "image"},
            model_snapshot={"model_id": "provider-cost-legacy-only"},
            result={"final_text": "legacy"},
            cost_frozen=9,
            cost_settled=9,
            started_at=now,
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(operation)
        db.flush()
        db.add(GatewayCall(
            user_id=uid,
            kind="reverse",
            model_id="provider-cost-legacy-only",
            status="ok",
            detail={"operation_id": operation.id, "cost_credits": 4},
            created_at=now,
        ))
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/admin/usage/reverse-operations",
        params={"model": "provider-cost-legacy-only"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["economics"]["cost_status"] == "unavailable"
    assert body["economics"]["provider_cost_credits"] == 0
    assert body["economics"]["gross_profit_credits"] is None
    assert body["economics"]["gross_margin_rate"] is None
    assert body["by_model_quality"][0]["cost_status"] == "unavailable"
    assert body["by_model_quality"][0]["gross_profit_credits"] is None
    assert body["model_costs"][0]["cost_credits"] == 4

    csv_response = client.get(
        "/api/admin/usage/reverse-operations",
        params={"model": "provider-cost-legacy-only", "format": "csv"},
        headers=headers,
    )
    rows = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
    all_row = next(row for row in rows if row["dimension"] == "all")
    assert all_row["cost_status"] == "unavailable"
    assert all_row["gross_profit_credits"] == ""
    assert all_row["gross_margin_rate"] == ""


def test_reverse_quality_cost_completeness_is_independent_per_dimension(
    client,
    make_user,
    auth,
):
    phone = "13900000046"
    uid = make_user(phone, balance=1000, admin=True)
    headers = auth(phone)
    now = datetime(2099, 5, 4, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        image = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-provider-cost-complete",
            request_fingerprint="5" * 64,
            target="image",
            analysis_focus="provider_cost_mix",
            asset_url="http://x/complete.png",
            status="succeeded",
            progress=100,
            request_context={"source_type": "image"},
            model_snapshot={"model_id": "provider-cost-complete"},
            result={"final_text": "complete"},
            cost_frozen=10,
            cost_settled=10,
            started_at=now,
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        video = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-provider-cost-partial",
            request_fingerprint="6" * 64,
            target="video",
            analysis_focus="provider_cost_mix",
            asset_url="http://x/partial.mp4",
            status="succeeded",
            progress=100,
            request_context={"source_type": "video"},
            model_snapshot={"model_id": "provider-cost-partial"},
            result={"video_analysis": {"source": {"duration_seconds": 8}}},
            cost_frozen=12,
            cost_settled=12,
            started_at=now,
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add_all([image, video])
        db.flush()
        db.add_all([
            GatewayCall(
                user_id=uid,
                kind="reverse",
                model_id="provider-cost-complete",
                status="ok",
                detail={
                    "operation_id": image.id,
                    "provider_cost_status": "complete",
                    "provider_cost_credits": 2,
                    "cost_credits": 10,
                },
                created_at=now,
            ),
            GatewayCall(
                user_id=uid,
                kind="reverse",
                model_id="provider-cost-partial",
                status="ok",
                detail={
                    "operation_id": video.id,
                    "provider_cost_status": "partial",
                    "provider_cost_known_credits": 1,
                    "cost_credits": 12,
                },
                created_at=now,
            ),
        ])
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/admin/usage/reverse-operations",
        params={"focus": "provider_cost_mix"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    economics = body["economics"]
    assert economics["cost_status"] == "partial"
    assert economics["provider_cost_credits"] == 3
    assert economics["gross_profit_credits"] is None
    assert economics["gateway_cost_coverage_rate"] == 1

    focus_row = body["by_focus"][0]
    assert focus_row["cost_status"] == "partial"
    assert focus_row["provider_cost_credits"] == 3
    assert focus_row["gross_profit_credits"] is None

    media_rows = {row["media_type"]: row for row in body["by_media_type"]}
    assert media_rows["image"]["cost_status"] == "complete"
    assert media_rows["image"]["gross_profit_credits"] == 8
    assert media_rows["video"]["cost_status"] == "partial"
    assert media_rows["video"]["gross_profit_credits"] is None


def test_reverse_quality_explicit_zero_provider_cost_is_complete(
    client,
    make_user,
    auth,
):
    phone = "13900000047"
    uid = make_user(phone, balance=1000, admin=True)
    headers = auth(phone)
    now = datetime(2099, 5, 5, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-provider-cost-zero",
            request_fingerprint="7" * 64,
            target="image",
            analysis_focus="provider_cost_zero",
            asset_url="http://x/zero.png",
            status="succeeded",
            progress=100,
            request_context={"source_type": "image"},
            model_snapshot={"model_id": "provider-cost-zero"},
            result={"final_text": "zero"},
            cost_frozen=7,
            cost_settled=7,
            started_at=now,
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(operation)
        db.flush()
        db.add(GatewayCall(
            user_id=uid,
            kind="reverse",
            model_id="provider-cost-zero",
            status="ok",
            detail={
                "operation_id": operation.id,
                "provider_cost_status": "complete",
                "provider_cost_credits": 0,
                "cost_credits": 7,
            },
            created_at=now,
        ))
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/admin/usage/reverse-operations",
        params={"model": "provider-cost-zero"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["economics"]["cost_status"] == "complete"
    assert body["economics"]["provider_cost_credits"] == 0
    assert body["economics"]["gross_profit_credits"] == 7
    assert body["economics"]["gross_margin_rate"] == 1
    assert body["by_model_quality"][0]["cost_status"] == "complete"


def test_usage_report_does_not_count_refunds_as_negative_spend(client, make_user, auth):
    phone = "13900009041"
    uid = make_user(phone, balance=1000, admin=True)
    h = auth(phone)
    db = SessionLocal()
    try:
        credits.consume(db, uid, 10, biz_type="reverse", biz_ref=1, note="reverse")
        credits.refund_consumed(db, uid, 10, biz_type="reverse", biz_ref=1, note="manual refund")
    finally:
        db.close()

    report = client.get("/api/admin/usage/report", headers=h).json()
    row = next(x for x in report["per_user"] if x["phone"] == phone)
    assert row["spend_credits"] == 10


def test_usage_report_attributes_generation_spend_to_finished_date(client, make_user, auth):
    uid = make_user("13900000139", balance=1000, admin=True)
    h = auth("13900000139")
    freeze_dt = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    finish_dt = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        user.balance_credits = 990
        db.add(GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
            prompt={"final_text": "x"},
            params={"n": 1},
            cost_frozen=10,
            cost_settled=6,
            finished_at=finish_dt,
        ))
        db.add(CreditTransaction(
            user_id=uid,
            type="freeze",
            change=-10,
            balance_after=990,
            biz_type="gen_task",
            biz_ref=999,
            created_at=freeze_dt,
        ))
        db.add(CreditTransaction(
            user_id=uid,
            type="settle",
            change=4,
            balance_after=994,
            biz_type="gen_task",
            biz_ref=999,
            created_at=finish_dt,
        ))
        db.commit()
    finally:
        db.close()

    jan1 = client.get(
        "/api/admin/usage/report?start=2026-01-01T00:00:00%2B00:00&end=2026-01-01T23:59:59%2B00:00",
        headers=h,
    ).json()
    jan2 = client.get(
        "/api/admin/usage/report?start=2026-01-02T00:00:00%2B00:00&end=2026-01-02T23:59:59%2B00:00",
        headers=h,
    ).json()

    row1 = next(x for x in jan1["per_user"] if x["phone"] == "13900000139")
    row2 = next(x for x in jan2["per_user"] if x["phone"] == "13900000139")
    assert row1["spend_credits"] == 0
    assert row2["spend_credits"] == 6
    assert jan1["daily"] == []
    assert jan2["daily"] == [{"date": "2026-01-02", "spend_credits": 6}]


def test_usage_report_end_date_includes_full_day(client, make_user, auth):
    uid = make_user("13900000149", balance=1000, admin=True)
    h = auth("13900000149")
    finish_dt = datetime(2026, 6, 19, 18, 30, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        db.add(GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
            prompt={"final_text": "x"},
            params={"n": 1},
            cost_frozen=10,
            cost_settled=6,
            finished_at=finish_dt,
        ))
        db.commit()
    finally:
        db.close()

    report = client.get(
        "/api/admin/usage/report?start=2026-06-19&end=2026-06-19",
        headers=h,
    ).json()

    row = next(x for x in report["per_user"] if x["phone"] == "13900000149")
    assert row["spend_credits"] == 6
    daily = next(x for x in report["daily"] if x["date"] == "2026-06-19")
    assert daily["spend_credits"] >= 6


def test_image_generation_logs_gateway_call(client, make_user, auth, quote_and_generate):
    make_user("13900000031", balance=1000)
    h = auth("13900000031")
    r = quote_and_generate({
        "source_asset_url": "http://x/y.png", "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "image", "stage": "preview", "instruction": "x",
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        rows = db.query(GatewayCall).filter(GatewayCall.kind == "image").all()
        assert rows, "an image generation must be logged"
    finally:
        db.close()


def test_usage_and_audit_logging_do_not_commit_caller_transaction(client, make_user):
    uid = make_user("13900000143", balance=1000)
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        user.nickname = "dirty-but-uncommitted"
        usage.record_call(
            db,
            kind="image",
            model_id="mock-image",
            user_id=uid,
            status="ok",
            detail={"case": "isolation"},
        )
        audit.log(
            db,
            user_id=uid,
            action="transaction_isolation_check",
            detail={"case": "isolation"},
        )
        db.rollback()
    finally:
        db.close()

    check = SessionLocal()
    try:
        assert check.get(User, uid).nickname is None
        assert check.query(GatewayCall).filter(
            GatewayCall.user_id == uid,
            GatewayCall.detail == {"case": "isolation"},
        ).count() == 1
        assert check.query(AuditLog).filter(
            AuditLog.user_id == uid,
            AuditLog.action == "transaction_isolation_check",
        ).count() == 1
    finally:
        check.close()


def test_failed_image_generation_logs_gateway_call(
    client, make_user, auth, monkeypatch, quote_and_generate
):
    make_user("13900000034", balance=1000)
    h = auth("13900000034")

    def fail(*_args, **_kwargs):
        raise gateway.GatewayError("bad image request")

    monkeypatch.setattr("app.services.gateway.gen_image", fail)
    r = quote_and_generate({
        "source_asset_url": "http://x/y.png", "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "image", "stage": "preview", "instruction": "x",
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        rows = db.query(GatewayCall).filter(
            GatewayCall.kind == "image",
            GatewayCall.status == "failed",
        ).all()
        assert rows
        assert "bad image request" in rows[-1].detail["error"]
    finally:
        db.close()


def test_video_reverse_requires_cover_confirmation_in_mock(client, make_user, auth):
    make_user("13900000032", balance=1000)
    h = auth("13900000032")
    # Mock mode has no extracted keyframes. Even with a supplied cover, the
    # compatibility endpoint must not silently downgrade a video analysis.
    r = client.post("/api/prompt/reverse", json={
        "asset_url": "http://x/clip.mp4", "target": "video",
        "fallback_image": "http://x/cover.jpg",
    }, headers=h)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["status"] == "needs_confirmation"
    assert detail["operation_id"] > 0
    assert detail["confirmation_expires_at"]


def test_video_reverse_uses_fixed_operation_price(client, make_user, auth, monkeypatch):
    make_user("13900009042", balance=1000, admin=True)
    h = auth("13900009042")
    assert client.put("/api/admin/models", json={
        "use": "vision",
        "model_id": "mock-vision",
        "cost_credits": 7,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    monkeypatch.setattr("app.config.settings.mock_mode", False)
    monkeypatch.setattr("app.config.settings.gateway_base_url", "https://gateway.test")
    monkeypatch.setattr("app.config.settings.gateway_api_key", "sk-test")
    monkeypatch.setattr(video_frames, "available", lambda: True)
    seen = {}

    def fake_sample(*_a, **kwargs):
        seen["n"] = kwargs.get("n")
        seen["preset"] = kwargs.get("preset")
        return video_frames.VideoSample(
            frames=tuple(
                video_frames.SampledVideoFrame(jpeg=value, timestamp_seconds=float(index))
                for index, value in enumerate((b"a", b"b", b"c"))
            ),
            source=video_frames.VideoMetadata(duration_seconds=10.0),
        )

    monkeypatch.setattr(video_frames, "sample_video", fake_sample)
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda refs, *_a, **_k: {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": None,
            "latency_ms": 1,
            "ref_count": len(refs),
        },
    )
    r = client.post("/api/prompt/reverse", json={
        "asset_url": "http://x/clip.mp4", "target": "video",
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["n"] == 14
    assert seen["preset"] == "standard"
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 995


def test_video_reverse_returns_authoritative_source_analysis(client, make_user, auth, monkeypatch):
    make_user("13900009052", balance=1000, admin=True)
    h = auth("13900009052")
    assert client.put("/api/admin/models", json={
        "use": "vision",
        "model_id": "mock-vision",
        "cost_credits": 7,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    monkeypatch.setattr("app.config.settings.mock_mode", False)
    monkeypatch.setattr("app.config.settings.gateway_base_url", "https://gateway.test")
    monkeypatch.setattr("app.config.settings.gateway_api_key", "sk-test")
    monkeypatch.setattr(video_frames, "available", lambda: True)
    sampled = video_frames.VideoSample(
        frames=(
            video_frames.SampledVideoFrame(jpeg=b"first", timestamp_seconds=0.0),
            video_frames.SampledVideoFrame(jpeg=b"middle", timestamp_seconds=4.2),
            video_frames.SampledVideoFrame(jpeg=b"last", timestamp_seconds=10.004),
        ),
        source=video_frames.VideoMetadata(
            width=720,
            height=960,
            duration_seconds=10.054,
            fps=23.0,
            has_audio=True,
        ),
    )
    monkeypatch.setattr(video_frames, "sample_video", lambda *_a, **_k: sampled)
    seen = {}

    def fake_reverse(refs, *_a, **kwargs):
        seen["refs"] = refs
        seen["video_analysis"] = kwargs.get("video_analysis")
        return {
            "structured": {"主体": "水感精华广告"},
            "final_text": "3:4竖版，10秒水感精华广告",
            "shots": [{"start_seconds": 0, "end_seconds": 2.2, "visual": "液滴入水"}],
            "usage": None,
            "latency_ms": 1,
        }

    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)
    request_body = {
        "client_request_id": "video-analysis-replay-001",
        "asset_url": "http://x/serum-ad.mp4",
        "target": "video",
    }
    r = client.post("/api/prompt/reverse", json=request_body, headers=h)

    assert r.status_code == 200, r.text
    analysis = r.json()["video_analysis"]
    assert analysis["source"] == {
        "width": 720,
        "height": 960,
        "ratio": "3:4",
        "duration_seconds": 10.054,
        "fps": 23.0,
        "has_audio": True,
        "audio_analyzed": False,
    }
    assert analysis["sampled_frames"] == [
        {"index": 1, "timestamp_seconds": 0.0, "absolute_timestamp_seconds": 0.0},
        {"index": 2, "timestamp_seconds": 4.2, "absolute_timestamp_seconds": 4.2},
        {
            "index": 3,
            "timestamp_seconds": 10.004,
            "absolute_timestamp_seconds": 10.004,
        },
    ]
    assert analysis["shots"][0]["end_seconds"] == 2.2
    assert seen["video_analysis"] == {
        "source": analysis["source"],
        "sampled_frames": analysis["sampled_frames"],
        "analysis_mode": "keyframes",
        "reference_context": [
            {"role": "frame", "source_type": "video", **frame}
            for frame in analysis["sampled_frames"]
        ],
    }
    assert len(seen["refs"]) == 3
    replay = client.post("/api/prompt/reverse", json=request_body, headers=h)
    assert replay.status_code == 200, replay.text
    assert replay.json()["video_analysis"] == analysis


def test_video_analysis_presets_match_product_frame_ranges():
    assert video_analysis.frame_count_for_duration(10, "fast") == 4
    assert video_analysis.frame_count_for_duration(10, "standard") == 8
    assert video_analysis.frame_count_for_duration(10, "fine") == 14
    assert video_analysis.frame_count_for_duration(10, "ultra") == 22
    assert video_analysis.frame_count_for_duration(120, "fast") == 5
    assert video_analysis.frame_count_for_duration(120, "standard") == 10
    assert video_analysis.frame_count_for_duration(120, "fine") == 17
    assert video_analysis.frame_count_for_duration(120, "ultra") == 27
    assert video_analysis.frame_count_for_duration(300, "fast") == 8
    assert video_analysis.frame_count_for_duration(300, "standard") == 14
    assert video_analysis.frame_count_for_duration(300, "fine") == 22
    assert video_analysis.frame_count_for_duration(300, "ultra") == 36


def test_video_reverse_rejects_video_url_for_image_target(client, make_user, auth):
    make_user("13900000033", balance=1000)
    h = auth("13900000033")
    r = client.post("/api/prompt/reverse", json={
        "asset_url": "http://x/clip.mp4", "target": "image",
    }, headers=h)
    assert r.status_code == 400
    assert "视频素材仅支持视频反推" in r.text


def test_keyframe_sampling_blocked_url_returns_empty():
    # SSRF-blocked / unreachable source must degrade gracefully, never raise
    assert video_frames.sample_keyframes("http://127.0.0.1/x.mp4", n=2) == []


def test_video_sample_download_rejects_compressed_response(monkeypatch):
    class FakeResponse:
        is_redirect = False
        headers = {
            "content-type": "video/mp4",
            "content-encoding": "gzip",
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            raise AssertionError("compressed responses should be rejected before status/body handling")

        def iter_bytes(self):
            yield b"\x00\x00\x00\x18ftypmp42"

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, method, url):
            assert method == "GET"
            assert url == "https://example.com/clip.mp4"
            return FakeResponse()

    seen = {}

    def fake_pinned_client(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return FakeClient()

    monkeypatch.setattr(video_frames, "assert_safe_url", lambda _url: None)
    monkeypatch.setattr(video_frames, "pinned_client", fake_pinned_client)

    assert video_frames._download_capped("https://example.com/clip.mp4") is None
    assert seen["headers"]["Accept-Encoding"] == "identity"


def test_keyframe_sampling_rejects_non_video_bytes(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames, "_download_capped", lambda *_a, **_k: b"<html>not video</html>")
    assert video_frames.sample_keyframes("http://x/not-video.mp4", n=2) == []


def test_keyframe_sampling_uses_full_duration_for_long_video(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames, "_download_capped", lambda *_a, **_k: b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(video_frames, "_duration_seconds", lambda _path: 900.0)
    stamps = []

    def fake_grab(_src, ts, dst):
        stamps.append(ts)
        with open(dst, "wb") as f:
            f.write(b"jpg")
        return True

    monkeypatch.setattr(video_frames, "_grab_frame", fake_grab)

    assert video_frames.sample_keyframes("http://x/long.mp4", n=3) == [b"jpg", b"jpg", b"jpg"]
    assert stamps == [0.0, 449.975, 899.95]


def test_uniform_keyframe_timestamps_cover_the_full_duration_evenly():
    assert video_frames._uniform_timestamps(20.0, 4) == [0.0, 6.65, 13.3, 19.95]
    assert video_frames._uniform_timestamps(900.0, 3) == [0.0, 449.975, 899.95]


def test_keyframe_sampling_preset_uses_downloaded_duration(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames, "_download_capped", lambda *_a, **_k: b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(video_frames, "_duration_seconds", lambda _path: 10.0)
    monkeypatch.setattr(video_frames, "_scene_change_timestamps", lambda *_a, **_k: [])
    stamps = []

    def fake_grab(_src, ts, dst):
        stamps.append(ts)
        with open(dst, "wb") as f:
            f.write(b"jpg")
        return True

    monkeypatch.setattr(video_frames, "_grab_frame", fake_grab)

    assert video_frames.sample_keyframes("http://x/short.mp4", n=36, preset="fine") == [b"jpg"] * 14
    assert len(stamps) == 14


def test_fine_sampling_gives_each_detected_short_shot_three_frames(monkeypatch, tmp_path):
    monkeypatch.setattr(video_frames, "probe_media", lambda *_args: {
        "width": 608,
        "height": 1080,
        "duration_seconds": 10.054,
        "fps": 24.0,
        "has_audio": True,
    })
    monkeypatch.setattr(
        video_frames,
        "_scene_change_timestamps",
        lambda *_args: [1.25, 2.75, 5.583, 6.375, 7.542, 8.375],
    )

    def fake_grab(_src, timestamp, destination):
        with open(destination, "wb") as output:
            output.write(f"frame-{timestamp}".encode())
        return timestamp

    monkeypatch.setattr(video_frames, "_grab_frame_with_backoff", fake_grab)
    source = tmp_path / "washcloth.mp4"
    source.write_bytes(b"video")

    sample = video_frames._sample_video_from_file(
        str(source),
        n=36,
        preset="fine",
    )

    counts: dict[int, int] = {}
    for frame in sample.frames:
        counts[frame.detected_shot_index] = counts.get(frame.detected_shot_index, 0) + 1
    assert len(sample.frames) == 21
    assert counts == {index: 3 for index in range(1, 8)}


def test_keyframe_sampling_backs_off_when_container_tail_has_no_decodable_frame(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames, "probe_media", lambda _path: {})
    monkeypatch.setattr(video_frames, "_scene_change_timestamps", lambda *_a, **_k: [])
    attempts = []

    def fake_grab(_src, ts, dst):
        attempts.append(round(ts, 3))
        if ts > 9.95:
            return False
        with open(dst, "wb") as f:
            f.write(b"jpg")
        return True

    monkeypatch.setattr(video_frames, "_grab_frame", fake_grab)
    sample = video_frames._sample_video_from_file(
        "/tmp/video.mp4",
        n=3,
        duration=10.054,
    )

    assert len(sample.frames) == 3
    assert attempts[-2:] == [10.004, 9.904]
    assert sample.frames[-1].timestamp_seconds == 9.904


def test_scene_change_detection_spreads_cuts_across_full_output(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")

    class Result:
        stdout = "\n".join(
            f"frame:{index} pts_time:{index * 0.8}"
            for index in range(1, 13)
        )

    monkeypatch.setattr(video_frames.subprocess, "run", lambda *_a, **_k: Result())
    stamps = video_frames._scene_change_timestamps("/tmp/video.mp4", limit=4)

    assert len(stamps) == 4
    assert stamps[0] <= 2.4
    assert stamps[-1] >= 8.0


def test_keyframe_sampling_prefers_scene_changes_then_uniform_fill(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames, "_download_capped", lambda *_a, **_k: b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(video_frames, "_duration_seconds", lambda _path: 20.0)
    monkeypatch.setattr(video_frames, "_scene_change_timestamps", lambda *_a, **_k: [2.0, 9.0])
    stamps = []

    def fake_grab(_src, ts, dst):
        stamps.append(ts)
        with open(dst, "wb") as f:
            f.write(b"jpg")
        return True

    monkeypatch.setattr(video_frames, "_grab_frame", fake_grab)

    assert video_frames.sample_keyframes("http://x/cuts.mp4", n=4) == [b"jpg"] * 4
    assert stamps == [0.0, 2.0, 9.0, 19.95]


def test_keyframe_sampling_covers_full_duration_when_early_scene_cuts_fill_budget(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames, "_download_capped", lambda *_a, **_k: b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(video_frames, "probe_media", lambda _path: {
        "width": 1920,
        "height": 1080,
        "duration_seconds": 300.0,
        "fps": 24.0,
        "has_audio": False,
    })
    monkeypatch.setattr(
        video_frames,
        "_scene_change_timestamps",
        lambda *_a, **_k: [2.0, 4.0, 8.0, 12.0, 18.0, 24.0, 30.0, 160.0, 240.0],
    )
    stamps = []

    def fake_grab(_src, ts, dst):
        stamps.append(ts)
        with open(dst, "wb") as f:
            f.write(b"jpg")
        return True

    monkeypatch.setattr(video_frames, "_grab_frame", fake_grab)

    sample = video_frames.sample_video("http://x/long-ad.mp4", n=8)

    assert sample is not None
    assert sample.frames[0].timestamp_seconds == 0.0
    assert sample.frames[-1].timestamp_seconds == 299.95
    assert any(frame.timestamp_seconds >= 150 for frame in sample.frames[1:-1])
    assert stamps == [frame.timestamp_seconds for frame in sample.frames]


def test_keyframe_sampling_keeps_temporal_coverage_when_all_scene_cuts_are_early(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames, "_download_capped", lambda *_a, **_k: b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(video_frames, "probe_media", lambda _path: {
        "width": 1920,
        "height": 1080,
        "duration_seconds": 300.0,
        "fps": 24.0,
        "has_audio": False,
    })
    monkeypatch.setattr(
        video_frames,
        "_scene_change_timestamps",
        lambda *_a, **_k: [2.0, 4.0, 8.0, 12.0, 18.0, 24.0, 30.0],
    )

    def fake_grab(_src, _ts, dst):
        with open(dst, "wb") as f:
            f.write(b"jpg")
        return True

    monkeypatch.setattr(video_frames, "_grab_frame", fake_grab)

    sample = video_frames.sample_video("http://x/early-cuts.mp4", n=8)

    assert sample is not None
    stamps = [frame.timestamp_seconds for frame in sample.frames]
    assert stamps[0] == 0.0
    assert stamps[-1] == 299.95
    assert any(120 <= stamp <= 180 for stamp in stamps)
    assert any(220 <= stamp < 299.95 for stamp in stamps)
    assert max(right - left for left, right in zip(stamps, stamps[1:], strict=False)) <= 70


def test_keyframe_sampling_busy_returns_empty(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames.settings, "reverse_video_acquire_timeout_seconds", 0)
    assert video_frames._SAMPLE_SEMAPHORE.acquire(blocking=False)
    try:
        monkeypatch.setattr(
            video_frames,
            "_download_capped",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not download")),
        )
        assert video_frames.sample_keyframes("http://x/clip.mp4", n=2) == []
    finally:
        video_frames._SAMPLE_SEMAPHORE.release()


def test_keyframe_duration_uses_ffprobe(monkeypatch):
    monkeypatch.setattr(video_frames, "FFPROBE", "/usr/bin/ffprobe")

    def fake_run(args, **_kwargs):
        assert "-show_entries" in args
        return SimpleNamespace(stdout="8.5\n")

    monkeypatch.setattr(video_frames.subprocess, "run", fake_run)
    assert video_frames._duration_seconds("/tmp/clip.mp4") == 8.5
