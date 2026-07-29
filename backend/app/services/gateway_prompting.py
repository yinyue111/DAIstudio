"""Compatibility facade for reverse-prompt templates and normalization.

The implementation is split by responsibility, but legacy imports and
monkeypatch targets remain available from this module.
"""
from __future__ import annotations

from . import gateway_prompt_audio as _audio
from . import gateway_prompt_validation as _validation
from . import gateway_prompt_video_shots as _video_shots
from . import gateway_prompt_visual as _visual
from .compat_facade import install_assignment_forwarding as _install_assignment_forwarding

_CHILD_MODULES = (_visual, _validation, _audio, _video_shots)

# Re-export private helpers as well as public functions. Existing tests and
# gateway adapters historically imported both from this module.
for _module in _CHILD_MODULES:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)

_install_assignment_forwarding(__name__, _CHILD_MODULES)

__all__ = tuple(
    name
    for name in globals()
    if not name.startswith("__")
    and name not in {"_CHILD_MODULES", "_install_assignment_forwarding"}
)
