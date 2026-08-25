"""The README's metadata table must name the keys the parser actually reads.

That table is a contract: a producer builds a `DeviceManifest` by following it.
It had drifted, and the failure modes were both bad. Two documented keys did not
exist (`pv`'s `nameplate-capacity-w`, `evse`'s `product-name`), so a manifest
built by following the table raised. Three more were accepted and silently
dropped, so `info/model` was simply absent from the published tree with no error
anywhere.

The table is also the PyPI project page, since `readme = "README.md"`.

So this reads both sides rather than restating either: the table out of
`README.md`'s own bytes, and the keys out of `manifest_physics.py`'s AST. A
transcription of either would pass while the thing it mirrors said something
else, which is exactly how the drift survived.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"
PHYSICS = Path(__file__).resolve().parents[1] / "src" / "ebus_panel_sim" / "manifest_physics.py"

# The helpers in manifest_physics that take a metadata key as their first string
# argument. Adding a reader without adding it here would make this test blind, so
# the set is asserted against the module below.
_READERS = frozenset(
    {
        "_require",
        "_req_str",
        "_req_int",
        "_req_float",
        "_req_csv_ints",
        "_opt_str",
        "_opt_int",
        "_opt_float",
        "_opt_bool",
    }
)

# Backticked tokens in the table that are values, defaults or types rather than
# metadata keys. Anything else backticked must be a key the parser reads.
_NOT_KEYS = frozenset(
    {
        "flat",
        "parent-child",
        "upstream",
        "downstream",
        "upstream-of-lugs",
        "downstream-of-lugs",
        "controllable",
        "non-controllable",
        "hybrid",
        "ac-coupled",
        "UPSTREAM",
        "IN_PANEL",
        "len(tab-numbers) > 1",
        "entity_class",
        "DeviceManifest",
        "DeviceInstance",
    }
)

# `\s*` on the trailing column: `lugs` has no optional keys, so its cell is empty.
# A stricter pattern silently skipped that row, which is the failure mode this
# whole file exists to prevent, so `test_every_documented_class_was_matched`
# asserts the row count rather than trusting the regex.
_ROW = re.compile(r"^\| `(?P<cls>[a-z]+)` \|\s*(?P<req>.*?)\s*\|\s*(?P<opt>.*?)\s*\|$", re.M)
_TICKED = re.compile(r"`([^`]+)`")


def _documented() -> dict[str, set[str]]:
    """Every backticked token in the table, per entity_class, minus known values."""
    out: dict[str, set[str]] = {}
    for m in _ROW.finditer(README.read_text()):
        toks = set(_TICKED.findall(m["req"])) | set(_TICKED.findall(m["opt"]))
        out[m["cls"]] = {t for t in toks if t not in _NOT_KEYS}
    return out


def _keys_read_by(
    fn: ast.FunctionDef, helpers: dict[str, ast.FunctionDef], seen: frozenset[str]
) -> set[str]:
    """The metadata keys `fn` reads, following calls into module-level helpers.

    The indirection matters: collapsing three copies of a read into one shared
    helper (`_feed`) is an ordinary refactor, and a walker that only looked
    inside `_parse_*` would have quietly stopped seeing `feed` and started
    reporting it as documented-but-never-read. Recursion is bounded by `seen`.
    """
    keys: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if name in helpers and name not in seen:
            keys |= _keys_read_by(helpers[name], helpers, seen | {name})
            continue
        if name not in _READERS and name != "get":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                keys.add(arg.value)
                break
    return keys


def _parsed() -> dict[str, set[str]]:
    """Every metadata key `manifest_physics` reads, per entity_class.

    Keyed off the `_parse_<class>` naming convention, so a new device class is
    covered the moment its parser follows it.
    """
    tree = ast.parse(PHYSICS.read_text())
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    helpers = {
        f.name: f for f in fns if f.name not in _READERS and not f.name.startswith("_parse_")
    }
    return {
        fn.name.removeprefix("_parse_"): _keys_read_by(fn, helpers, frozenset())
        for fn in fns
        if fn.name.startswith("_parse_")
    }


def test_every_documented_class_was_matched() -> None:
    """The table and the parser must describe the same set of device classes.

    Guards the other blind spot: a row this file's regex fails to match reads as
    "documents nothing", which would make the assertions below pass for the wrong
    reason. `lugs` (empty optional column) did exactly that on the first draft.
    """
    assert set(_documented()) == set(_parsed()), (
        f"table rows {sorted(_documented())} vs parsers {sorted(_parsed())}"
    )


def test_the_reader_set_is_current() -> None:
    """Guards this test's own blind spot: a new `_req_*`/`_opt_*` helper that
    `_READERS` does not name would make every assertion below vacuous."""
    tree = ast.parse(PHYSICS.read_text())
    defined = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and re.fullmatch(r"_(req|opt|require)\w*", n.name)
    }
    assert defined <= _READERS, f"unrecognised metadata readers: {sorted(defined - _READERS)}"


def test_every_documented_key_is_one_the_parser_reads() -> None:
    """The failure that raised: `pv`'s `nameplate-capacity-w` and `evse`'s
    `product-name` were documented as required and did not exist."""
    parsed, documented = _parsed(), _documented()
    ghosts = {
        cls: sorted(keys - parsed.get(cls, set()))
        for cls, keys in documented.items()
        if keys - parsed.get(cls, set())
    }
    assert not ghosts, f"README documents keys the parser never reads: {ghosts}"


def test_every_key_the_parser_reads_is_documented() -> None:
    """The failure that stayed silent: `schema-topology`, `part-number` and
    `dipole` were read and absent from the table, so nobody could set them.

    `feed-circuit-id` is excluded deliberately: it is a deprecated alias for
    `feed`, and documenting it would entrench a second name for one concept.
    """
    parsed, documented = _parsed(), _documented()
    deprecated = {"feed-circuit-id"}
    missing = {
        cls: sorted(keys - documented.get(cls, set()) - deprecated)
        for cls, keys in parsed.items()
        if keys - documented.get(cls, set()) - deprecated
    }
    assert not missing, f"parser reads keys the README does not document: {missing}"
