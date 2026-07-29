"""Compatibility support for modules split behind a re-export facade."""
from __future__ import annotations

import sys
from collections.abc import Iterable
from types import ModuleType


def install_assignment_forwarding(
    facade_name: str,
    children: Iterable[ModuleType],
) -> None:
    """Forward assignments on a legacy facade to matching child bindings."""
    facade = sys.modules[facade_name]
    child_modules = tuple(dict.fromkeys(children))
    missing = object()
    targets: dict[str, tuple[ModuleType, ...]] = {}
    for name, value in vars(facade).copy().items():
        if name.startswith("__"):
            continue
        matches = tuple(
            child
            for child in child_modules
            if child is not facade and getattr(child, name, missing) is value
        )
        if matches:
            targets[name] = matches

    class AssignmentForwardingModule(ModuleType):
        def __setattr__(self, name: str, value: object) -> None:
            super().__setattr__(name, value)
            for child in targets.get(name, ()):
                setattr(child, name, value)

    facade.__class__ = AssignmentForwardingModule
