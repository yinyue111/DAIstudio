"""Server-authoritative quote and accounting coverage for legacy HD unlocks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import CreditTransaction, GenAsset, GenerationQuote, ModelConfig, User


def _locked_asset(user_id: int, *, unlock_cost: int) -> int:
    with SessionLocal() as db:
        model = (
            db.query(ModelConfig)
            .filter(ModelConfig.use == "image", ModelConfig.enabled.is_(True))
            .order_by(ModelConfig.is_default.desc(), ModelConfig.id.asc())
            .first()
        )
        assert model is not None
        model.unlock_cost = int(unlock_cost)
        asset = GenAsset(
            task_id=None,
            user_id=user_id,
            type="image",
            preview_url="/media/legacy-preview.png",
            hd_url="/media/legacy-hd.png",
            watermarked=True,
            unlocked=False,
            moderation_status="active",
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)
        return int(asset.id)


def _quote(client, headers: dict, asset_id: int, request_id: str) -> dict:
    response = client.post(
        "/api/quotes",
        headers=headers,
        json={
            "kind": "asset_unlock",
            "client_request_id": request_id,
            "request": {"asset_id": asset_id},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_paid_asset_unlock_requires_quote_and_replay_is_idempotent(
    client, make_user, auth
):
    user_id = make_user("13979500001", balance=50)
    headers = auth("13979500001")
    asset_id = _locked_asset(user_id, unlock_cost=7)

    missing = client.post(f"/api/assets/{asset_id}/unlock", headers=headers)
    assert missing.status_code == 422, missing.text
    assert missing.json()["detail"]["code"] == "QUOTE_REQUIRED"

    quote = _quote(client, headers, asset_id, "asset-unlock-paid-0001")
    assert quote["kind"] == "asset_unlock"
    assert quote["estimated_credits"] == 7
    unlocked = client.post(
        f"/api/assets/{asset_id}/unlock",
        headers=headers,
        json={"quote_id": quote["quote_id"]},
    )
    assert unlocked.status_code == 200, unlocked.text
    assert unlocked.json()["unlocked"] is True

    replay = client.post(
        f"/api/assets/{asset_id}/unlock",
        headers=headers,
        json={"quote_id": quote["quote_id"]},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == asset_id

    with SessionLocal() as db:
        user = db.get(User, user_id)
        persisted = db.get(GenerationQuote, quote["quote_id"])
        transactions = (
            db.query(CreditTransaction)
            .filter_by(user_id=user_id, biz_type="unlock", biz_ref=asset_id)
            .order_by(CreditTransaction.id)
            .all()
        )
        assert user is not None and user.balance_credits == 43
        assert user.frozen_credits == 0
        assert persisted is not None and persisted.status == "consumed"
        assert persisted.consumed_ref_type == "asset_unlock"
        assert persisted.consumed_ref_id == asset_id
        assert [row.type for row in transactions] == ["freeze", "settle"]


def test_asset_unlock_quote_rejects_tampering_cross_user_and_expiry(
    client, make_user, auth
):
    owner_id = make_user("13979500002", balance=50)
    other_id = make_user("13979500003", balance=50)
    owner_headers = auth("13979500002")
    other_headers = auth("13979500003")
    first_asset_id = _locked_asset(owner_id, unlock_cost=9)
    second_asset_id = _locked_asset(owner_id, unlock_cost=9)
    other_asset_id = _locked_asset(other_id, unlock_cost=9)

    quote = _quote(client, owner_headers, first_asset_id, "asset-unlock-owner-0002")
    tampered = client.post(
        f"/api/assets/{second_asset_id}/unlock",
        headers=owner_headers,
        json={"quote_id": quote["quote_id"]},
    )
    assert tampered.status_code == 409, tampered.text
    assert tampered.json()["detail"]["code"] == "QUOTE_MISMATCH"

    cross_user = client.post(
        f"/api/assets/{other_asset_id}/unlock",
        headers=other_headers,
        json={"quote_id": quote["quote_id"]},
    )
    assert cross_user.status_code == 404, cross_user.text
    assert cross_user.json()["detail"]["code"] == "QUOTE_NOT_FOUND"

    with SessionLocal() as db:
        row = db.get(GenerationQuote, quote["quote_id"])
        assert row is not None
        row.status = "canceled"
        db.commit()
    canceled_replacement = _quote(
        client,
        owner_headers,
        first_asset_id,
        "asset-unlock-owner-0002",
    )
    assert canceled_replacement["quote_id"] != quote["quote_id"]
    assert canceled_replacement["status"] == "active"

    expired_quote = _quote(
        client,
        owner_headers,
        second_asset_id,
        "asset-unlock-expired-0003",
    )
    with SessionLocal() as db:
        row = db.get(GenerationQuote, expired_quote["quote_id"])
        assert row is not None
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    expired = client.post(
        f"/api/assets/{second_asset_id}/unlock",
        headers=owner_headers,
        json={"quote_id": expired_quote["quote_id"]},
    )
    assert expired.status_code == 409, expired.text
    assert expired.json()["detail"]["code"] == "QUOTE_EXPIRED"
    expired_replacement = _quote(
        client,
        owner_headers,
        second_asset_id,
        "asset-unlock-expired-0003",
    )
    assert expired_replacement["quote_id"] != expired_quote["quote_id"]
    assert expired_replacement["status"] == "active"

    with SessionLocal() as db:
        assert db.get(GenAsset, first_asset_id).unlocked is False
        assert db.get(GenAsset, second_asset_id).unlocked is False
        assert db.get(GenAsset, other_asset_id).unlocked is False


def test_asset_unlock_insufficient_balance_keeps_quote_and_asset_retryable(
    client, make_user, auth
):
    user_id = make_user("13979500004", balance=3)
    headers = auth("13979500004")
    asset_id = _locked_asset(user_id, unlock_cost=8)
    quote = _quote(client, headers, asset_id, "asset-unlock-poor-0004")

    response = client.post(
        f"/api/assets/{asset_id}/unlock",
        headers=headers,
        json={"quote_id": quote["quote_id"]},
    )
    assert response.status_code == 400, response.text
    with SessionLocal() as db:
        user = db.get(User, user_id)
        asset = db.get(GenAsset, asset_id)
        persisted = db.get(GenerationQuote, quote["quote_id"])
        assert user is not None and user.balance_credits == 3
        assert user.frozen_credits == 0
        assert asset is not None and asset.unlocked is False
        assert persisted is not None and persisted.status == "active"
        assert db.query(CreditTransaction).filter_by(user_id=user_id).count() == 0


def test_asset_unlock_rejects_persisted_quote_snapshot_tampering(
    client,
    make_user,
    auth,
):
    user_id = make_user("13979500006", balance=50)
    headers = auth("13979500006")
    asset_id = _locked_asset(user_id, unlock_cost=6)
    quote = _quote(client, headers, asset_id, "asset-unlock-tampered-0006")

    with SessionLocal() as db:
        row = db.get(GenerationQuote, quote["quote_id"])
        assert row is not None
        pricing = dict(row.pricing_snapshot or {})
        pricing["quoted_credits"] = 1
        row.pricing_snapshot = pricing
        db.commit()

    response = client.post(
        f"/api/assets/{asset_id}/unlock",
        headers=headers,
        json={"quote_id": quote["quote_id"]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "QUOTE_SNAPSHOT_INVALID"
    with SessionLocal() as db:
        user = db.get(User, user_id)
        asset = db.get(GenAsset, asset_id)
        persisted = db.get(GenerationQuote, quote["quote_id"])
        assert user is not None and user.balance_credits == 50
        assert user.frozen_credits == 0
        assert asset is not None and asset.unlocked is False
        assert persisted is not None and persisted.status == "active"
        assert db.query(CreditTransaction).filter_by(user_id=user_id).count() == 0


def test_free_asset_unlock_remains_backward_compatible_without_quote(
    client, make_user, auth
):
    user_id = make_user("13979500005", balance=10)
    headers = auth("13979500005")
    asset_id = _locked_asset(user_id, unlock_cost=0)

    response = client.post(f"/api/assets/{asset_id}/unlock", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["unlocked"] is True
    with SessionLocal() as db:
        user = db.get(User, user_id)
        assert user is not None and user.balance_credits == 10
        assert user.frozen_credits == 0
        assert db.query(GenerationQuote).filter_by(user_id=user_id).count() == 0
        assert db.query(CreditTransaction).filter_by(user_id=user_id).count() == 0
