"""Engines: the things that actually execute a step.

Two are planned, and the split is the whole reason Regulator exists in the
shape it does.

``api`` drives splunkd's REST search endpoints. It answers the search-tier
question: how fast does the cluster return answers, how many can it run at
once, and where does queueing start. One worker process handles hundreds of
virtual users, because the work is almost entirely waiting on I/O.

``browser`` (Phase 2) drives a real headless Chromium against Splunk Web. It
answers a different question that the REST path cannot: what does a user
actually experience, including the JavaScript bundle, the panel rendering and
the time to the first pixel of data. It is far more expensive per virtual user,
roughly 150 to 300 MB of memory per browser context, so the realistic shape of
a large test is a big API cohort for search-tier load alongside a small browser
cohort for the experience measurement.

They are kept behind one protocol so the scheduler never learns the difference,
and so a scenario can mix both in a single persona.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .api import ApiEngine
from .base import Engine, StepContext, TargetCapabilities
from .browser import BrowserEngine, BrowserUnavailable

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only
    from ..config import Config

__all__ = [
    "ApiEngine",
    "BrowserEngine",
    "BrowserUnavailable",
    "Engine",
    "StepContext",
    "TargetCapabilities",
    "get_engine",
    "ENGINE_NAMES",
]

ENGINE_NAMES = ("api", "browser")


def get_engine(name: str, config: "Config") -> Engine:
    """Build an engine by name.

    Raises rather than falling back to a default. Silently substituting an
    engine would mean a scenario that asked for browser measurements quietly
    produced REST measurements instead, and the numbers would look perfectly
    plausible while answering the wrong question.
    """
    key = (name or "").strip().lower()
    if key == "api":
        return ApiEngine(config)
    if key == "browser":
        return BrowserEngine(config)
    raise ValueError(
        f"unknown engine {name!r}, expected one of: {', '.join(ENGINE_NAMES)}"
    )
