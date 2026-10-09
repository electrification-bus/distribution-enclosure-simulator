"""Each reference capture, republished from its own definition, against the capture.

``tests/fixtures/r202639-<handle>-tree-v1.json`` is a masked tree of a SPAN panel
on ``spanos3/r202639/03``, and ``r202639-<handle>.yaml`` and ``.ticks.yaml`` are the
definition and the 60 one-second ticks recorded with it. The two MAIN 32 captures
are held to the same bar: ``main32_r202639`` with its definition and ticks, and
the r202633 capture ``main32-tree-v1.json``, which has neither, through the
definition ``panel-sim-capture`` writes from it and one tick sampled from it.

The emitter publishes the definition through every tick, and the retained tree
is compared with the capture device by device: ``$description`` keys, type and
children, nodes, declared properties and their datatype, settable, unit, format
and name, valued versus unvalued, the literal form of each value, and the sign
of each power.
Masked identifiers, time-varying magnitudes and ``connection/count`` may differ.
Devices are aligned by role, not id: a branch circuit by its spaces and name, a
circuit device without ``info/spaces`` by its ordinal, lugs by direction, and any
other device by its type's ordinal.

A capture the emitter does not reproduce yet is held to the exact number of
differences it still shows, so a change in either direction fails until the
count is updated, and a capture reproduced in full drops out of the table."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from ebus_panel_sim import (
    Emitter,
    PanelDefinition,
    SetterRegistry,
    TickInputs,
    load_definition,
    load_ticks,
)
from ebus_panel_sim.capture import (
    Device,
    Tree,
    definition_from_tree,
    ticks_from_samples,
    tree_from_retained,
    tree_from_snapshot,
)

from .conftest import PahoRecorder

_FIXTURES = Path(__file__).parent / "fixtures"
_HANDLES = sorted(
    path.name.removesuffix("-tree-v1.json") for path in _FIXTURES.glob("r202639-*-tree-v1.json")
)
_MAIN32_R202639 = "main32_r202639"
_MAIN32_R202633 = "main32-r202633"
# The differences each capture still shows, exactly; a capture absent here must
# show none.
_RESIDUAL: dict[str, int] = {
    "r202639-a": 15,
    "r202639-b": 15,
    "r202639-c": 15,
    "r202639-d": 28,
    "r202639-e": 17,
    _MAIN32_R202639: 7,
    _MAIN32_R202633: 28,
}
_NOT_COMPARED = frozenset({"connection/count"})
_ATTRIBUTES = ("datatype", "settable", "unit", "format", "name")
_SIGNED = re.compile(r"^(meter/active-power|power-flows/.+)$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


def _mapping(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _nodes(device: Device) -> dict[str, dict[str, object]]:
    return {
        node: _mapping(body) for node, body in _mapping(device.description.get("nodes")).items()
    }


def _declarations(body: dict[str, object]) -> dict[str, dict[str, object]]:
    return {key: _mapping(decl) for key, decl in _mapping(body.get("properties")).items()}


def _roles(tree: Tree) -> dict[str, str]:
    """Role to device id, so a masked id never decides which devices are compared."""
    roles: dict[str, str] = {}
    ordinals: Counter[str] = Counter()
    for device_id in sorted(tree):
        device = tree[device_id]
        if device.type == "circuit" and not device.declares("info/spaces"):
            ordinals["meter-only circuit"] += 1
            role = f"meter-only circuit #{ordinals['meter-only circuit']}"
        elif device.type == "circuit":
            role = f"circuit {device.value('info/spaces')} {device.value('info/name')}"
        elif device.type == "lugs":
            role = f"lugs {device.value('info/direction')}"
        else:
            ordinals[device.type] += 1
            role = f"{device.type} #{ordinals[device.type]}"
        if role in roles:
            ordinals[role] += 1
            role = f"{role} ~{ordinals[role]}"
        roles[role] = device_id
    return roles


def _shape(value: str, declaration: dict[str, object]) -> str:
    """What a value looks like on the wire, by its declared datatype."""
    datatype = declaration.get("datatype")
    if value == "":
        return "empty"
    if datatype in ("integer", "float"):
        if value.startswith("-0") and value.strip("-0.") == "":
            return "negative zero"
        if not _NUMBER.match(value):
            return f"not a number ({datatype})"
        if "e" in value.lower():
            return "exponent"
        if "." not in value:
            return "integer"
        return f"{len(value.split('.', 1)[1])} decimal places"
    if datatype == "boolean":
        return "boolean" if value in ("true", "false") else "not a boolean"
    if datatype == "enum":
        return "in format" if value in str(declaration.get("format", "")).split(",") else "out"
    if datatype == "json":
        try:
            json.loads(value)
        except ValueError:
            return "not json"
        return "json"
    return "string"


def _sign(value: str) -> int:
    """The sign of a power, or 0 when it is under a watt or not a number."""
    try:
        number = float(value)
    except ValueError:
        return 0
    return 0 if abs(number) < 1.0 else (1 if number > 0 else -1)


def _children_by_type(tree: Tree, device: Device) -> Counter[str]:
    children = device.description.get("children", [])
    return Counter(tree[c].type for c in children if c in tree)


def _device_differences(captured: Device, published: Device) -> list[str]:
    out: list[str] = []
    top_c = set(captured.description) - {"nodes"}
    top_p = set(published.description) - {"nodes"}
    if top_c != top_p:
        out.append(f"$description keys: {sorted(top_c ^ top_p)}")
    for key in ("homie", "type"):
        if captured.description.get(key) != published.description.get(key):
            out.append(f"$description {key}")
    nodes_c, nodes_p = _nodes(captured), _nodes(published)
    if set(nodes_c) != set(nodes_p):
        out.append(f"nodes: {sorted(set(nodes_c) ^ set(nodes_p))}")
    for node in sorted(set(nodes_c) & set(nodes_p)):
        if nodes_c[node].get("type") != nodes_p[node].get("type"):
            out.append(f"node {node} type")
        decl_c, decl_p = _declarations(nodes_c[node]), _declarations(nodes_p[node])
        for key in sorted(set(decl_c) ^ set(decl_p)):
            if f"{node}/{key}" not in _NOT_COMPARED:
                where = "capture" if key in decl_c else "emitter"
                out.append(f"{node}/{key}: declared only by the {where}")
        for key in sorted(set(decl_c) & set(decl_p)):
            path = f"{node}/{key}"
            if path in _NOT_COMPARED:
                continue
            out.extend(
                f"{path} {attribute}: {decl_c[key].get(attribute)!r} vs "
                f"{decl_p[key].get(attribute)!r}"
                for attribute in _ATTRIBUTES
                if decl_c[key].get(attribute) != decl_p[key].get(attribute)
            )
            value_c, value_p = captured.properties.get(path), published.properties.get(path)
            if (value_c is None) != (value_p is None):
                where = "capture" if value_c is not None else "emitter"
                out.append(f"{path}: valued only by the {where}")
            elif value_c is not None and value_p is not None:
                shape_c, shape_p = _shape(value_c, decl_c[key]), _shape(value_p, decl_p[key])
                if shape_c != shape_p:
                    out.append(f"{path} shape: {shape_c} ({value_c}) vs {shape_p} ({value_p})")
                signs = (_sign(value_c), _sign(value_p))
                if _SIGNED.match(path) and all(signs) and signs[0] != signs[1]:
                    out.append(f"{path} sign: {value_c} vs {value_p}")
    return out


def _differences(captured: Tree, published: Tree) -> list[str]:
    roles_c, roles_p = _roles(captured), _roles(published)
    out = [f"device {role}: only in the capture" for role in sorted(set(roles_c) - set(roles_p))]
    out += [f"device {role}: only in the emitter" for role in sorted(set(roles_p) - set(roles_c))]
    for role in sorted(set(roles_c) & set(roles_p)):
        device_c, device_p = captured[roles_c[role]], published[roles_p[role]]
        if _children_by_type(captured, device_c) != _children_by_type(published, device_p):
            out.append(f"{role}: children by type")
        out += [f"{role}: {d}" for d in _device_differences(device_c, device_p)]
    return out


def _capture(handle: str) -> Tree:
    name = "main32-tree-v1.json" if handle == _MAIN32_R202633 else f"{handle}-tree-v1.json"
    return tree_from_snapshot(json.loads((_FIXTURES / name).read_text(encoding="utf-8")))


def _inputs(handle: str, captured: Tree) -> tuple[PanelDefinition, list[TickInputs]]:
    """The definition and ticks a capture is republished from."""
    if handle == _MAIN32_R202633:
        definition, _ = definition_from_tree(captured, mask=False)
        return definition, ticks_from_samples(captured, [(0.0, captured)], mask=False)
    return load_definition(_FIXTURES / f"{handle}.yaml"), load_ticks(
        _FIXTURES / f"{handle}.ticks.yaml"
    )


def _published(rec: PahoRecorder, definition: PanelDefinition, ticks: list[TickInputs]) -> Tree:
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    for tick in ticks:
        emitter.publish_tick(tick)
    return tree_from_retained(rec.retained)


def test_every_reference_capture_is_found() -> None:
    assert [f"r202639-{h}" for h in "abcde"] == _HANDLES
    for handle in _HANDLES:
        assert (_FIXTURES / f"{handle}.yaml").is_file()
        assert (_FIXTURES / f"{handle}.ticks.yaml").is_file()


@pytest.mark.parametrize("handle", [*_HANDLES, _MAIN32_R202639, _MAIN32_R202633])
def test_the_emitter_differs_from_the_capture_by_its_residual(
    rec: PahoRecorder, handle: str
) -> None:
    captured = _capture(handle)
    differences = _differences(captured, _published(rec, *_inputs(handle, captured)))
    expected = _RESIDUAL.get(handle, 0)
    assert len(differences) == expected, (
        f"{len(differences)} differences, expected {expected}:\n" + "\n".join(differences)
    )
