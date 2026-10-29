"""Shared pytest fixtures.

Environment isolation: the repo ships a real `.env` at the root, and several
application entrypoints (the signal-service lifespan, the macro/news refresh
CLIs) legitimately bridge `.env` into ``os.environ`` via
``atlas_shared.load_env`` at startup. When a test boots the FastAPI app through
``TestClient``, that lifespan runs and mutates the process-global
``os.environ`` for the remainder of the session — which would leak real API
keys into later tests that exercise the "no key → fallback" paths
(e.g. ``test_newsapi_source``).

The autouse fixture below snapshots ``os.environ`` before each test and
restores it afterward, so any env mutation — from a lifespan, a stray
``os.environ[...] = ...``, or a plain assignment — is undone at test boundary.
Tests that use pytest's ``monkeypatch`` still work (monkeypatch's own teardown
runs first; this is a belt-and-suspenders outer layer).
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_environ():
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        # Restore to the exact pre-test state: drop added keys, restore changed
        # ones, re-add removed ones.
        os.environ.clear()
        os.environ.update(snapshot)
