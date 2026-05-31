"""CLI-runnability guard: every `python -m <module>` referenced in user-facing
docs must actually be importable and have a callable entrypoint. Catches the
class of bug where a README says `python -m foo` but `foo/__main__.py` is
missing.

We check the docs for the patterns, then import-and-resolve each one without
running it. No subprocess, no DB needed."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC_SOURCES = [
    ROOT / "README.md",
    ROOT / "docs" / "runbooks" / "OPERATIONS.md",
]

# Picks up `python -m foo`, `python -m foo.bar`, etc.
_CLI_RE = re.compile(r"python -m ([a-zA-Z_][\w.]*)")


def _documented_modules() -> set[str]:
    out: set[str] = set()
    for src in DOC_SOURCES:
        if not src.exists():
            continue
        out.update(_CLI_RE.findall(src.read_text()))
    return out


def test_every_documented_cli_resolves() -> None:
    bad: list[tuple[str, str]] = []
    for mod_name in sorted(_documented_modules()):
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            bad.append((mod_name, f"import failed: {e}"))
            continue

        # Two valid shapes:
        # 1. submodule like `foo.bar` — needs a `main` callable.
        # 2. package like `foo` — needs an importable `foo.__main__` module.
        if "." in mod_name:
            entry = getattr(mod, "main", None)
            if not callable(entry):
                bad.append((mod_name, "no callable `main` in module"))
        else:
            try:
                importlib.import_module(f"{mod_name}.__main__")
            except Exception as e:
                bad.append((mod_name, f"`{mod_name}.__main__` not importable: {e}"))

    assert not bad, "Broken `python -m` entrypoints in docs:\n  " + "\n  ".join(
        f"{m}: {why}" for m, why in bad
    )
