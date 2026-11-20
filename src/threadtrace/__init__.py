"""ThreadTrace: local static analysis for Python projects."""

from __future__ import annotations

from typing import Any

__all__ = ["HybridHarness", "__version__"]
__version__ = "0.7.0a1"


def __getattr__(name: str) -> Any:
    if name == "HybridHarness":
        from .hybrid import HybridHarness
        return HybridHarness
    raise AttributeError(name)
