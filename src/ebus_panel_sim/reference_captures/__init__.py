"""The reference captures: masked trees of real SPAN panels, each shipped with the
definition and the ticks recorded with it.

They are package data, so a consumer replays a real panel from the release it
pins rather than from a copy of this repository's files::

    capture = load_reference_capture("r202639-a")
    emitter = Emitter.from_definition(capture.definition, SetterRegistry())
    emitter.start()
    for tick in capture.ticks:
        emitter.publish_tick(tick)

``capture.tree`` is what the panel itself published, to compare the result with.
Each capture is three files here: ``<name>.yaml`` (``load_definition``),
``<name>.ticks.yaml`` (``load_ticks``) and ``<name>-tree-v1.json``
(``tree_from_snapshot``)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import as_file, files

from ebus_panel_sim.capture import Tree, tree_from_snapshot
from ebus_panel_sim.definition import PanelDefinition, load_definition, load_ticks
from ebus_panel_sim.exceptions import UnknownReferenceCaptureError
from ebus_panel_sim.tick_inputs import TickInputs

_TREE_SUFFIX = "-tree-v1.json"


@dataclass(frozen=True, slots=True)
class ReferenceCapture:
    """One reference capture: the tree the panel published, and the definition and
    ticks recorded with it, which republish that tree."""

    name: str
    definition: PanelDefinition
    ticks: tuple[TickInputs, ...]
    tree: Tree


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
    with as_file(data / f"{name}.yaml") as definition_path:
        definition = load_definition(definition_path)
    with as_file(data / f"{name}.ticks.yaml") as ticks_path:
        ticks = tuple(load_ticks(ticks_path))
    snapshot = json.loads((data / f"{name}{_TREE_SUFFIX}").read_text(encoding="utf-8"))
    return ReferenceCapture(
        name=name, definition=definition, ticks=ticks, tree=tree_from_snapshot(snapshot)
    )
