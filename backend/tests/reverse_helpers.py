from __future__ import annotations

from typing import Any


def quote_reverse(client, body: dict[str, Any], *, headers=None):
    request = dict(body)
    request.pop("quote_id", None)
    request_id = request["client_request_id"]
    return client.post(
        "/api/quotes",
        json={
            "kind": "reverse",
            "client_request_id": request_id,
            "request": request,
        },
        headers=headers,
    )


def post_reverse(client, body: dict[str, Any], *, headers=None):
    quote = quote_reverse(client, body, headers=headers)
    if quote.status_code != 201:
        return quote
    return client.post(
        "/api/prompt/reverse-operations",
        json={**body, "quote_id": quote.json()["quote_id"]},
        headers=headers,
    )


def quote_reverse_batch(client, body: dict[str, Any], *, headers=None):
    request = dict(body)
    request.pop("quote_id", None)
    request_id = request["client_request_id"]
    return client.post(
        "/api/quotes",
        json={
            "kind": "reverse_batch",
            "client_request_id": request_id,
            "request": request,
        },
        headers=headers,
    )


def post_reverse_batch(client, body: dict[str, Any], *, headers=None):
    quote = quote_reverse_batch(client, body, headers=headers)
    if quote.status_code != 201:
        return quote
    return client.post(
        "/api/prompt/reverse-batches",
        json={**body, "quote_id": quote.json()["quote_id"]},
        headers=headers,
    )


def quote_reverse_retry(
    client,
    operation_id: int,
    body: dict[str, Any],
    *,
    headers=None,
):
    request = {
        "reverse_operation_id": int(operation_id),
        "client_request_id": body["client_request_id"],
    }
    if body.get("model_config_id") is not None:
        request["model_config_id"] = body["model_config_id"]
    return client.post(
        "/api/quotes",
        json={
            "kind": "reverse",
            "client_request_id": body["client_request_id"],
            "request": request,
        },
        headers=headers,
    )


def post_reverse_retry(
    client,
    operation_id: int,
    body: dict[str, Any],
    *,
    headers=None,
):
    quote = quote_reverse_retry(client, operation_id, body, headers=headers)
    if quote.status_code != 201:
        return quote
    return client.post(
        f"/api/prompt/reverse-operations/{operation_id}/retry",
        json={**body, "quote_id": quote.json()["quote_id"]},
        headers=headers,
    )
