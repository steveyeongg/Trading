"""The swing job's real dependencies, shared by the scheduler (``main.py``) and "Run scan now".

:func:`make_swing_deps` is the one factory: Alpaca for the bars and the calendar, the database
stores, Telegram, the clock, the checkout's committed validation files, and the API's context
cache as ``on_context``, so every job run (scheduled, caught up or manual) leaves its target
session's :class:`~swing_engine.scan.ScanContext` for the stock and positions pages
(:mod:`signal_service.swing_context`). It lives apart from ``main.py`` and ``swing_routes.py`` so
both can use it without importing each other.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from swing_engine.data.alpaca import AlpacaData
from swing_engine.job import JobDeps, live_deps

from signal_service.swing_context import set_context


def repo_root() -> Path:
    """The checkout this file lives in (where ``ml/validation`` is), else the working directory.

    The swing job and the track record read the setups' committed validation reports from here;
    a wrong root would silently mark every setup unproven, so it does not depend on where the
    API was started.
    """
    root = Path(__file__).resolve().parents[4]
    return root if (root / "ml").is_dir() else Path.cwd()


def make_swing_deps() -> JobDeps:
    """The real dependencies of the swing job, with the API's context cache as ``on_context``.

    Raises (e.g. ``AlpacaError`` without Alpaca keys) when they cannot be built: the scheduler
    records why and the API starts anyway; the "Run scan now" route answers 503.
    """
    return dataclasses.replace(live_deps(AlpacaData(), repo_root()), on_context=set_context)
