from __future__ import annotations

import pytest
from redis.exceptions import ResponseError
from starlette.websockets import WebSocketDisconnect

from app.db import SessionLocal
from app.models import ReverseOperation
from app.redis_client import redis_client
from app.routers import ws as ws_router


def _create_terminal_operation(user_id: int) -> int:
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=user_id,
            request_fingerprint="f" * 64,
            target="image",
            asset_url="https://cdn.example.com/reference.jpg",
            status="succeeded",
            progress=100,
            result={"structured": {"主体": "杯子"}, "final_text": "红色杯子"},
            cost_frozen=0,
            cost_settled=2,
            charged_credits=2,
        )
        db.add(operation)
        db.commit()
        db.refresh(operation)
        return int(operation.id)
    finally:
        db.close()


def _assert_ws_rejected(client, path: str, expected_code: int) -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(path):
            pass
    assert closed.value.code == expected_code


def test_reverse_ws_ticket_legacy_redis_fallback_is_atomic(monkeypatch):
    key = "ws:reverse-ticket:legacy-ticket"

    class LegacyRedis:
        def __init__(self):
            self.values = {key: "7:11:3"}
            self.eval_calls = 0

        def getdel(self, _key):
            raise ResponseError("unknown command 'GETDEL'")

        def eval(self, script, key_count, ticket_key):
            assert "redis.call('GET'" in script
            assert "redis.call('DEL'" in script
            assert key_count == 1
            self.eval_calls += 1
            return self.values.pop(ticket_key, None)

        def get(self, _key):
            raise AssertionError("non-atomic GET fallback must not be used")

        def delete(self, _key):
            raise AssertionError("non-atomic DELETE fallback must not be used")

    legacy = LegacyRedis()
    monkeypatch.setattr(ws_router, "redis_client", legacy)

    assert ws_router._consume_reverse_ticket("legacy-ticket") == (7, 11, 3)
    assert ws_router._consume_reverse_ticket("legacy-ticket") is None
    assert legacy.eval_calls == 2


def test_reverse_ws_ticket_has_60_second_ttl_and_expires(
    client,
    make_user,
    auth,
):
    user_id = make_user("13710000901", balance=100)
    headers = auth("13710000901")
    operation_id = _create_terminal_operation(user_id)

    response = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/ws-ticket",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["expires_in"] == 60
    key = f"ws:reverse-ticket:{payload['ticket']}"
    assert 0 < redis_client.ttl(key) <= 60

    redis_client.delete(key)
    _assert_ws_rejected(
        client,
        f"/ws/prompt/reverse-operations/{operation_id}?ticket={payload['ticket']}",
        4401,
    )


def test_reverse_ws_ticket_is_owner_scoped_operation_scoped_and_one_time(
    client,
    make_user,
    auth,
):
    owner_id = make_user("13710000902", balance=100)
    make_user("13710000903", balance=100)
    owner_headers = auth("13710000902")
    other_headers = auth("13710000903")
    operation_id = _create_terminal_operation(owner_id)
    other_operation_id = _create_terminal_operation(owner_id)

    forbidden = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/ws-ticket",
        headers=other_headers,
    )
    assert forbidden.status_code == 404

    wrong_target_ticket = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/ws-ticket",
        headers=owner_headers,
    ).json()["ticket"]
    _assert_ws_rejected(
        client,
        f"/ws/prompt/reverse-operations/{other_operation_id}?ticket={wrong_target_ticket}",
        4404,
    )
    _assert_ws_rejected(
        client,
        f"/ws/prompt/reverse-operations/{operation_id}?ticket={wrong_target_ticket}",
        4401,
    )

    ticket = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/ws-ticket",
        headers=owner_headers,
    ).json()["ticket"]
    with client.websocket_connect(
        f"/ws/prompt/reverse-operations/{operation_id}?ticket={ticket}"
    ) as websocket:
        state = websocket.receive_json()
    assert state["id"] == operation_id
    assert state["status"] == "succeeded"
    assert state["cost_settled"] == 2

    _assert_ws_rejected(
        client,
        f"/ws/prompt/reverse-operations/{operation_id}?ticket={ticket}",
        4401,
    )
