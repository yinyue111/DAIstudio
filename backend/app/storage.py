"""Backwards-compatible shim. The real implementation lives in
``app.services.storage`` (imported as ``from ..services import storage``)."""
from .services.storage import (  # noqa: F401
    ROOT,
    key_from_url,
    local_path,
    public_url,
    save_bytes,
    save_stream,
)
