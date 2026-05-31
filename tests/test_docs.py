"""Docs drift guard: the SYSTEM map must reflect the real package set, and
every referenced doc path must exist."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "apps"
DOCS = ROOT / "docs"
SYSTEM_MD = DOCS / "architecture" / "SYSTEM.md"
CHANGELOG = ROOT / "CHANGELOG.md"
README = ROOT / "README.md"
DOCS_INDEX = DOCS / "README.md"


def _existing_app_dirs() -> set[str]:
    """The set of app package directories on disk (folders with a src/<pkg>)."""
    out: set[str] = set()
    for d in APPS.iterdir():
        if d.is_dir() and (d / "src").exists():
            out.add(d.name)
    return out


def test_system_map_lists_every_app_package() -> None:
    system = SYSTEM_MD.read_text()
    on_disk = _existing_app_dirs()
    missing = [name for name in on_disk if f"`apps/{name}`" not in system]
    assert not missing, f"SYSTEM.md missing rows for: {missing}"


def test_changelog_exists_and_is_versioned() -> None:
    text = CHANGELOG.read_text()
    # At least one semver-shaped heading.
    assert re.search(r"^## \d+\.\d+\.\d+", text, re.MULTILINE), "no version headings in CHANGELOG"


def test_docs_links_resolve() -> None:
    """Every relative link in the docs index, root README, and SYSTEM.md must
    point at an existing file."""
    for src in (DOCS_INDEX, README, SYSTEM_MD):
        text = src.read_text()
        for match in re.finditer(r"\]\(([^)#]+?)(#[^)]*)?\)", text):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (src.parent / target).resolve()
            assert resolved.exists(), f"{src.name} → missing link target: {target}"


def test_root_readme_does_not_still_claim_phase_0() -> None:
    text = README.read_text()
    assert "currently at **Phase 0**" not in text, "README still says we're at Phase 0"
