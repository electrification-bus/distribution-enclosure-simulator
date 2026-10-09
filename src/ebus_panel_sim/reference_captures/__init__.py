"""The reference captures: masked trees of real SPAN panels, each with a definition
and ticks that republish it.

They are package data, so a consumer replays a real panel from the release it
pins rather than from a copy of this repository's files::

    capture = load_reference_capture("r202639-a")
    emitter = Emitter.from_definition(capture.definition, SetterRegistry())
    emitter.start()
    for tick in capture.ticks:
        emitter.publish_tick(tick)

``capture.tree`` is what the panel itself published, to compare the result with.
Each capture is ``<name>-tree-v1.json`` here (``tree_from_snapshot``), with the
``<name>.yaml`` definition (``load_definition``) and ``<name>.ticks.yaml`` ticks
(``load_ticks``) recorded with it. A capture shipped as its tree alone has both
derived from the tree at load, as ``panel-sim-capture`` derives them, says so in
``derived_from_tree`` and keeps the derivation's ``notes``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import as_file, files

from ebus_panel_sim.capture import (
    CaptureNote,
    Tree,
    definition_from_tree,
    ticks_from_samples,
    tree_from_snapshot,
)
from ebus_panel_sim.definition import PanelDefinition, load_definition, load_ticks
from ebus_panel_sim.exceptions import UnknownReferenceCaptureError
from ebus_panel_sim.tick_inputs import TickInputs

_TREE_SUFFIX = "-tree-v1.json"


@dataclass(frozen=True, slots=True)
class ReferenceCapture:
    """One reference capture: the tree the panel published, and a definition and
    ticks that republish that tree.

    ``derived_from_tree`` is ``False`` when the definition and ticks were recorded
    with the capture. It is ``True`` when the capture is the tree alone: the
    definition is then ``definition_from_tree(tree, mask=False)`` (the tree is
    already masked) and the ticks are one tick sampled from the tree, the captured
    instant, so they hold no more than the tree does. ``notes`` then holds what
    ``definition_from_tree`` had to default or leave out (a battery's dispatch
    settings, lugs fed by another enclosure); it is empty for a recorded capture."""

    name: str
    definition: PanelDefinition
    ticks: tuple[TickInputs, ...]
    tree: Tree
    derived_from_tree: bool
    notes: tuple[CaptureNote, ...]


def reference_capture_names() -> tuple[str, ...]:
    """The name of every reference capture the package ships, sorted."""
    return tuple(
        sorted(
            entry.name.removesuffix(_TREE_SUFFIX)
            for entry in files(__name__).iterdir()
            if entry.name.endswith(_TREE_SUFFIX)
        )
    )


def load_reference_capture(name: str) -> ReferenceCapture:
    """Read the reference capture *name*, one of ``reference_capture_names()``.

    Every call reads the files afresh, so a caller may change what it gets. Raises
    ``UnknownReferenceCaptureError`` for any other name."""
    known = reference_capture_names()
    if name not in known:
        raise UnknownReferenceCaptureError(name, known)
    data = files(__name__)
    tree = tree_from_snapshot(
        json.loads((data / f"{name}{_TREE_SUFFIX}").read_text(encoding="utf-8"))
    )
    definition_file = data / f"{name}.yaml"
    if not definition_file.is_file():
        definition, notes = definition_from_tree(tree, mask=False)
        ticks = ticks_from_samples(tree, [(0.0, tree)], mask=False)
        return ReferenceCapture(
            name=name,
            definition=definition,
            ticks=tuple(ticks),
            tree=tree,
            derived_from_tree=True,
            notes=tuple(notes),
        )
    with as_file(definition_file) as definition_path:
        definition = load_definition(definition_path)
    with as_file(data / f"{name}.ticks.yaml") as ticks_path:
        ticks = load_ticks(ticks_path)
    return ReferenceCapture(
        name=name,
        definition=definition,
        ticks=tuple(ticks),
        tree=tree,
        derived_from_tree=False,
        notes=(),
    )
