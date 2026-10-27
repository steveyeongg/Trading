"""Pre-registration: the parameters and gate fixed in a committed file before out-of-sample runs.

``run_insample`` writes a draft ``ml/validation/<family>/<version>.prereg.json``. The person
running the validation reviews it and commits it, and only then will ``run_oos`` start: it calls
:func:`check_committed`, which refuses a file that is

* missing, or untracked by git (``git ls-files --error-unmatch``);
* different from its committed version (``git diff --quiet HEAD``), staged changes included;
* edited by hand: ``sha256`` is the digest of the canonical JSON of every other field (sorted
  keys, no whitespace), so a change that did not re-seal the file shows;
* a revision (any version but ``<family>-v1``) without a written hypothesis.

``protocol`` seals the rules outside the setup's parameters
(:func:`~swing_engine.validation.protocol.protocol_block`). A file written before it existed has
no ``protocol`` key: it still loads, its seal still verifies (a missing protocol is left out of
the digest), and :mod:`~swing_engine.validation.run` treats its rules as unverified.

git runs as a subprocess with an argument list, never through a shell.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PreregError(RuntimeError):
    """The pre-registration is missing, uncommitted, altered, or incomplete."""


@dataclass(frozen=True)
class Prereg:
    """One pre-registered out-of-sample run. ``params`` and ``gate`` are ``asdict`` of the
    setup parameters and :class:`~swing_engine.validation.protocol.GateThresholds`, and
    ``protocol`` is :func:`~swing_engine.validation.protocol.protocol_block` (None in a file
    written before it existed); dates are ISO strings and ``created_at`` is an ISO UTC timestamp."""

    family: str
    version: str
    params: dict[str, Any]
    gate: dict[str, Any]
    is_window: list[str]
    oos_start: str
    hypothesis: str
    created_at: str
    sha256: str
    protocol: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        *,
        family: str,
        version: str,
        params: dict[str, Any],
        gate: dict[str, Any],
        is_window: list[str],
        oos_start: str,
        hypothesis: str,
        created_at: str,
        protocol: dict[str, Any] | None,
    ) -> Prereg:
        """A sealed pre-registration: ``sha256`` computed from the other fields."""
        fields: dict[str, Any] = {
            "family": family,
            "version": version,
            "params": params,
            "gate": gate,
            "is_window": is_window,
            "oos_start": oos_start,
            "hypothesis": hypothesis,
            "created_at": created_at,
            "protocol": protocol,
        }
        return cls(**fields, sha256=_digest(fields))

    def is_sealed(self) -> bool:
        """True when ``sha256`` is still the digest of the other fields (no hand edit since)."""
        try:
            return _digest(dataclasses.asdict(self)) == self.sha256
        except (TypeError, ValueError):  # a field JSON cannot encode canonically (NaN, a set)
            return False


def _digest(fields: Mapping[str, Any]) -> str:
    """Hex sha256 of the canonical JSON of ``fields`` without ``sha256`` (sorted, compact).

    A ``protocol`` of None is left out too, so a file from before the protocol block verifies.
    """
    body = {
        k: v for k, v in fields.items() if k != "sha256" and not (k == "protocol" and v is None)
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_draft(path: Path, prereg: Prereg) -> None:
    """Write ``prereg`` to ``path`` as indented JSON, creating the directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dataclasses.asdict(prereg), indent=2, sort_keys=True, allow_nan=False)
    path.write_text(text + "\n", encoding="utf-8")


def load_prereg(path: Path) -> Prereg:
    """Read a pre-registration file (unchecked: see :func:`check_committed`)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return Prereg(**doc)
    except FileNotFoundError:
        raise PreregError(
            f"no pre-registration at {path}: run `python -m swing_engine insample` first"
        ) from None
    except (json.JSONDecodeError, TypeError) as exc:
        raise PreregError(f"{path} is not a valid pre-registration: {exc}") from None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        raise PreregError("git is required to verify pre-registration") from None


def code_state(repo: Path) -> dict[str, Any]:
    """``code_commit`` (``git rev-parse HEAD``) and ``code_dirty`` (tracked files differ from it).

    Either is None when git cannot tell (no repository, no commit yet). Untracked files are not
    counted: a report's own new files would otherwise mark every run dirty.
    """
    head = _git(repo, "rev-parse", "--verify", "HEAD")
    status = _git(repo, "status", "--porcelain", "--untracked-files=no")
    return {
        "code_commit": head.stdout.strip() if head.returncode == 0 else None,
        "code_dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def check_committed(path: Path, repo: Path) -> Prereg:
    """The pre-registration at ``path``, after checking it is committed, unchanged and complete.

    Raises :class:`PreregError` for each refusal listed in the module docstring.
    """
    prereg = load_prereg(path)
    try:
        rel = path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        raise PreregError(f"{path} is not inside the repository {repo}") from None

    tracked = _git(repo, "ls-files", "--error-unmatch", "--", rel)
    if tracked.returncode != 0:
        raise PreregError(
            f"{rel} is not committed: commit the pre-registration before the out-of-sample run"
            f" ({tracked.stderr.strip()})"
        )
    changed = _git(repo, "diff", "--quiet", "HEAD", "--", rel)
    if changed.returncode == 1:
        raise PreregError(
            f"{rel} differs from HEAD: commit it (or restore it) before the out-of-sample run"
        )
    if changed.returncode != 0:
        raise PreregError(f"cannot compare {rel} with HEAD: {changed.stderr.strip()}")

    if not prereg.is_sealed():
        raise PreregError(
            f"{rel} was edited after it was written: its sha256 does not match its contents"
        )
    if prereg.version != f"{prereg.family}-v1" and not prereg.hypothesis.strip():
        raise PreregError(
            f"{prereg.version} is a revision: its pre-registration needs a written hypothesis "
            "drawn from in-sample analysis"
        )
    return prereg
