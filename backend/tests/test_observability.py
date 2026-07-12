"""Request context sanitization and propagation."""

from app.observability import normalize_request_id


def test_normalize_request_id_accepts_bounded_safe_value():
    assert normalize_request_id("studio.request-123:worker") == "studio.request-123:worker"
    assert normalize_request_id("a" * 128) == "a" * 128


def test_normalize_request_id_rejects_log_and_header_injection():
    for value in (None, "", " leading", "trailing ", "line\nbreak", "snowman-\u2603", "a" * 129):
        assert normalize_request_id(value) is None


def test_invalid_request_id_is_replaced_before_response(client):
    supplied = "a" * 129
    response = client.get("/api/live", headers={"x-request-id": supplied})

    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    assert request_id != supplied
    assert normalize_request_id(request_id) == request_id
