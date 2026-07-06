"""Backwards-compatible shim. The real implementation lives in
``app.services.storage`` (imported as ``from ..services import storage``)."""
from .services.storage import (  # noqa: F401
    ROOT,
    download_to_local_temp,
    key_from_url,
    local_path,
    presigned_download_url,
    public_url,
    save_bytes,
    save_stream,
)
