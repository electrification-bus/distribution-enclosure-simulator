"""Guard the vendored spec capability catalogs against silent drift.

``src/ebus_panel_sim/wire/catalogs/*.json`` are byte copies of the eBus specification's
``capabilities/*.json``, vendored so the wire datatypes are single-sourced from the
spec (see ``wire/profile_loader.py``). Two things must stay honest:

1. ``.ebus-spec.json`` pins a version for each capability ebus-panel-sim implements; that
   pin must match the ``version`` in the vendored catalog it was copied from. This is
   self-contained and always runs.
2. The vendored copies must be byte-identical to the spec **at the commit this repo
   claims to be pinned to**, which is ``.ebus-spec.json``'s ``synced_commit``.

Point 2 used to compare against whatever ``../specification`` happened to be checked
out at, which measured the wrong thing three ways. It failed when the sibling clone
moved *ahead* of the pin, which is ordinary currency drift and not a defect in this
repo. It failed spuriously when the clone sat on an unrelated branch or a dirty tree.
And it passed falsely when the clone was itself stale at the pinned commit while the
real spec had moved on. Reading the source out of git at ``synced_commit`` instead
makes the check deterministic regardless of what the clone is doing, and makes it the
question actually worth asking: *are the bytes we vendored the bytes we say they are?*

That is an **integrity** invariant: always true, checkable at any time, and a genuine
failure when violated. It is deliberately not a **currency** check. Whether the spec
has moved past our pin is a separate question, one whose answer is normally "yes, a
little", and which must not fail a build. Nothing automatic checks currency yet; that
wants a scheduled job rather than a PR gate.

Set ``EBUS_SPEC_DIR`` to point at a specification clone anywhere; otherwise the
sibling ``../specification`` is used.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CATALOG_DIR = _ROOT / "src" / "ebus_panel_sim" / "wire" / "catalogs"
_LOCKFILE = _ROOT / ".ebus-spec.json"
_SPEC_DIR = Path(os.environ.get("EBUS_SPEC_DIR") or (_ROOT.parent / "specification"))


def _short_name(capability_urn: str) -> str:
    """energy.ebus.capability.shed-forecast -> shed-forecast."""
    return capability_urn.rsplit(".", 1)[-1]


def _vendored_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for path in sorted(_CATALOG_DIR.glob("*.json")):
        raw = json.loads(path.read_text())
        versions[_short_name(raw["capability"])] = raw["version"]
    return versions


def _synced_commit() -> str:
    return str(json.loads(_LOCKFILE.read_text())["synced_commit"])


def _git_show(spec_dir: Path, ref: str, repo_path: str) -> str | None:
    """Read one file out of the spec repo at a commit. None if it is not there."""
    result = subprocess.run(
        ["git", "-C", str(spec_dir), "show", f"{ref}:{repo_path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def test_lockfile_pins_match_vendored_catalog_versions() -> None:
    """Every capability the lockfile pins that is also vendored must agree on version.

    (A vendored-but-unpinned catalog like charge-limit is fine: it is staged for a
    migration the profiles don't reference yet.)"""
    pinned: dict[str, str] = json.loads(_LOCKFILE.read_text())["implements"]["capabilities"]
    vendored = _vendored_versions()
    for name, pin in pinned.items():
        assert name in vendored, f".ebus-spec.json pins {name!r} but no catalog is vendored"
        assert vendored[name] == pin, (
            f"{name}: lockfile pins {pin} but vendored catalog is {vendored[name]}"
        )


@pytest.mark.skipif(
    not (_SPEC_DIR / ".git").exists(),
    reason=(
        f"no specification clone at {_SPEC_DIR}; set EBUS_SPEC_DIR or check one out "
        "as a sibling to compare vendored catalogs against their source commit"
    ),
)
def test_vendored_catalogs_match_spec_at_synced_commit() -> None:
    """Each vendored catalog is byte-identical to the spec at ``synced_commit``.

    Not to the spec's current HEAD: that would be a currency check, and being behind
    the spec is a normal state rather than a defect.
    """
    commit = _synced_commit()

    # A clone that has never fetched the pinned commit cannot answer the question.
    # Say so loudly rather than skipping, because a silent skip here reads as a pass
    # and this is the check that would otherwise never run.
    have_commit = subprocess.run(
        ["git", "-C", str(_SPEC_DIR), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    assert have_commit.returncode == 0, (
        f"the specification clone at {_SPEC_DIR} does not have the pinned commit "
        f"{commit[:9]}. Fetch it (`git -C {_SPEC_DIR} fetch origin`) and re-run; the "
        "vendored catalogs cannot be verified against a commit that is not present."
    )

    drift: list[str] = []
    for path in sorted(_CATALOG_DIR.glob("*.json")):
        source = _git_show(_SPEC_DIR, commit, f"capabilities/{path.name}")
        if source is None:
            drift.append(f"{path.name}: does not exist in the spec at {commit[:9]}")
        elif path.read_text() != source:
            drift.append(f"{path.name}: differs from the spec at {commit[:9]}")

    assert not drift, (
        "vendored catalogs do not match the spec commit this repo pins.\n  "
        + "\n  ".join(drift)
        + "\n\nEither re-vendor from that commit, or if the intent was to adopt newer "
        "spec content, re-vendor and move synced_commit together."
    )
