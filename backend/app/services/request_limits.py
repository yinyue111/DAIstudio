"""Small request-size guards for public upload/callback entrypoints."""
from __future__ import annotations

from fastapi import HTTPException, Request


def enforce_content_length(request: Request, limit: int, message: str) -> None:
    header = request.headers.get("content-length")
    if not header:
        return
    try:
        size = int(header)
    except ValueError:
        raise HTTPException(400, "Content-Length 非法")
    if size > limit:
        raise HTTPException(413, message)


async def read_limited_body(request: Request, limit: int, message: str) -> bytes:
    """Read request.body() after header pre-checking.

    The app-level ASGI middleware enforces this limit while Starlette consumes
    the stream. This helper keeps route code explicit and protects direct unit
    calls that bypass middleware but still carry a Content-Length header.
    """
    enforce_content_length(request, limit, message)
    body = await request.body()
    if len(body) > limit:
        raise HTTPException(413, message)
    return body
